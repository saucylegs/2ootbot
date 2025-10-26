import os
import logging
import praw
import tweepy
import tomllib # Requires Python 3.11 or newer
import re
import requests
import xml.dom.minidom as xmldom
import subprocess
import time

with open("./config.toml", "rb") as f:
    CONFIG = tomllib.load(f)
with open("./secrets.toml", "rb") as f:
    SECRETS = tomllib.load(f)

MEDIA_FOLDER = os.path.abspath(CONFIG["media"]["media_folder"])
CACHE_FILE = os.path.abspath(CONFIG["media"]["cache_file"])

# Setting up logging
if CONFIG["logging"]["logfile"]:
    logging.basicConfig(filename=CONFIG["logging"]["logfile"], encoding="utf-8", level=CONFIG["logging"]["log_level"]*10)
else:
    logging.basicConfig(level=CONFIG["logging"]["log_level"]*10)

# Creating cache file & media folder if they do not exist
if CONFIG["media"]["get_media"] and not os.path.exists(MEDIA_FOLDER):
    logging.info(f"Media directory ({MEDIA_FOLDER}) does not yet exist; creating it")
    os.mkdir(MEDIA_FOLDER)
if not os.path.exists(CACHE_FILE):
    logging.info(f"Cache file ({CACHE_FILE}) does not yet exist; creating it")
    with open(CACHE_FILE, mode="xt", encoding="utf8") as file:
        file.write("reddit id,successful posts,time posted\n")


# === Error classes ===

class TootbotError(Exception):
    """
    Base class for errors raised by Tootbot code. Can represent an error extracting media, an invalid Reddit post, API errors, etc.
    """
    def __init__(self, message: str, relevant_objects:(dict[str, ]|None)=None, original_error:(BaseException|None)=None, severity=4):
        """
        Parameters:
            message (str): A helpful message to output to the console.
            relevant_objects (dict[str, Any]): A dictionary of things that may be useful for debugging this error, with a description for each item. e.g. {"Reddit Submission object": vars(RedditSubmissionObject)}
            original_error: If this error is instantiated from a try/except block, this should be the originally caught error object.
        """
        self.message = message
        self.severity = severity
        if relevant_objects:
            for k, v in relevant_objects.items():
                self.message += f"\n  {k}: {v}"
        if original_error:
            self.message += f"\n  Original error raised: {original_error}"
        self.log()
        super().__init__(self.message)

    def log(self):
        logging.log(self.severity * 10, self.message)

class ExtractionError(TootbotError):
    """Indicates that an error occured that prevented the media file from being downloaded."""

class UploadError(TootbotError):
    """Indicates that an error occured that prevented the media file from being uploaded to Twitter."""

class RepostError(TootbotError):
    """Indicates that the bot failed to post to a certain location."""

class InvalidSubmissionError(TootbotError):
    """Indicates that a Reddit post does not satisfy the parameters specified in the config file. The bot should thus move on to the next post."""
    def __init__(self, message: str, relevant_objects:(dict[str, ]|None)=None, original_error:(BaseException|None)=None):
        super().__init__(message, relevant_objects=relevant_objects, original_error=original_error, severity=3)


# === Media classes ===

class MediaFile:
    """A media file (image or video) attached to a Reddit submission.
    Has various methods for downloading and uploading the file."""

    def __init__(self, submission:(praw.reddit.Submission|None)=None, url:str=None, name:str=None, filepath:str=None, type=""):
        """
        Parameters:
            submission: The Reddit submission that this media file was originally attached to.
            url: A URL from which the file can be downloaded. Inferred from submission if not provided.
            name: The filename. Automatically inferred if not provided.
            filepath: The filepath of the local copy of the file. Should only be provided if it has been downloaded already.
            type: The content-type header (MIME type) of the file. Should be given for files that are not hosted by Reddit.
        """
        self.filepath = filepath
        self.type = type
        self.in_iteration = False
        if submission:
            self.submission = submission
            self.url = url if url else submission.url
        elif url:
            self.url = url
        self.name = name if name else self.generate_name()

    def is_downloaded(self) -> bool:
        """Returns whether this media file has been downloaded locally."""
        return self.filepath != None

    def delete(self):
        """Deletes the local copy of the file."""
        if self.filepath:
            try:
                os.remove(self.filepath)
                logging.info(f"Deleted file located at {self.filepath}")
            except FileNotFoundError:
                logging.warning(f"Tried to delete the file {self.filepath}, but could not find it. Was it already deleted?")

    def upload_to_twitter(self, client: tweepy.API) -> int|str:
        """Uploads a media file to Twitter. This must be done before using it in a Tweet. Note that this requires the older v1.1 API.
        Returns the Twitter media ID to refer to this file; this is twitter_id object attribute.
        All of the rest of the data returned by Twitter regarding the upload will be stored in the twitter_upload_info object attribute."""
        match self.type:
            case "video/mp4":
                media_category = "tweet_video"
            case "image/gif":
                media_category = "tweet_gif"
            case _:
                media_category = "tweet_image"
        try:
            upload = client.chunked_upload(self.filepath, media_category=media_category)
            self.twitter_upload_info = upload
            self.twitter_id = upload.media_id
        except BaseException as e:
            raise UploadError(f"There was an error when trying to upload the media file {self.name} to Twitter.", original_error=e)
        logging.info(f"Uploaded media file {self.name} to Twitter. Twitter Media ID: {self.twitter_id}")
        logging.debug(f"Uploaded file object attributes: {vars(self.twitter_upload_info)}")
        return self.twitter_id
    
    def generate_name(self) -> str:
        """Generates a name for the file based on its URL and possibly its MIME type."""
        urlmatch = re.search(r"\/(\w+)(\.[A-Za-z0-9.]+)?\/?$", self.url)
        if urlmatch:
            self.name = urlmatch.group(1)
            if urlmatch.group(2): # File extension extracted from URL
                self.name += urlmatch.group(2).lower()
            else: # Try to guess file extension based on MIME type, if available
                self.name += get_file_ext(self.type)
        else:
            self.name = "unnamed_media" + get_file_ext(self.type)
        return self.name

    def __str__(self):
        terms = [f"MediaFile {self.name}", f"from url {self.url}"]
        if self.submission:
            terms.append(f"belonging to Reddit submission {self.submission.id}")
        if self.filepath:
            terms.append(f"downloaded locally at {self.filepath}")
        return f"({ ', '.join(terms) })"
    
    # Iterator methods for convenience, so there doesn't have to be two separate cases for one MediaFile and a list[MediaFile].
    def __iter__(self):
        return self
    def __next__(self):
        if self.in_iteration:
            self.in_iteration = False
            raise StopIteration
        else:
            self.in_iteration = True
            return self

class VideoFile(MediaFile):
    def __init__(self, submission:(praw.reddit.Submission|None)=None, url=None, name=None, filepath=None, type="video/mp4"):
        super().__init__(submission, url, name, filepath, type)

    def download(self):
        """Downloads a local copy of the video. The local file can be referred to by this object's filepath attribute.
        Because Reddit separates the video and audio tracks, ffmpeg needs to be installed and usable from the command line so they can be recombined."""
        try:
            dash_url = self.submission.media["reddit_video"]["dash_url"]
        except (KeyError, TypeError) as e:
            raise ExtractionError("Tried to download a video, but no video data was found.", {"Reddit submission object": vars(self.submission)}, e)
        
        logging.info(f"Downloading the video {self.url}")
        # Reddit keeps their video and audio tracks separate and lists them in a .mpd (XML) file.
        # Download that file and add <BaseURL> to the XML so that ffmpeg knows where to download the files from.
        mpd = requests.get(dash_url).text
        doc = xmldom.parseString(mpd)
        new_element = doc.createElement("BaseURL")
        base_url = self.url 
        if base_url[-1] != "/":
            base_url += "/"
        new_element.appendChild( doc.createTextNode(base_url) )
        doc.documentElement.appendChild(new_element)
        mpd_file = os.path.join(MEDIA_FOLDER, "DASHPlaylist.mpd")
        with open(mpd_file, "w") as file:
            doc.writexml(file)

        logging.info(f"Downloaded video playlist to {mpd_file}")
        logging.info("Downloading the video/audio tracks and combining them with ffmpeg. This might take a while...")
        
        video_file = os.path.join(MEDIA_FOLDER, self.name)
        try:
            # Using ffmpeg to download the video and audio tracks and merge them into one mp4 file
            subprocess.run(["ffmpeg", "-i", mpd_file, video_file], capture_output=True, check=True)
        except subprocess.CalledProcessError as e:
            with open(mpd_file) as file:
                mpd_file_content = file.read()
            raise ExtractionError(f"ffmpeg failed to download the video for {video_file}.", 
                                  {
                                      "VideoFile object": self,
                                      "ffmpeg stderr": e.stderr,
                                      "DASHPlaylist.mpd file contents": mpd_file_content,
                                      "Reddit submission object": vars(self.submission)
                                  }, e)
        finally:
            os.remove(mpd_file) # .mpd file no longer needed
            logging.info("Deleted the mpd playlist file as it is no longer needed")
        
        logging.info(f"Downloaded video file to {video_file}")
        self.filepath = video_file
        self.size = os.path.getsize(self.filepath)

class ImageFile(MediaFile):
    def download(self):
        """Downloads a local copy of the image. The local file can be referred to by this object's filepath attribute."""
        logging.info(f"Downloading the image {self.url}")
        img_file = os.path.join(MEDIA_FOLDER, self.name)
            
        # Downloading image file
        try:
            req = requests.get(self.url, stream=True)
            self.type = req.headers["content-type"]
            with open(img_file, "wb") as file:
                for chunk in req.iter_content(chunk_size=None):
                    file.write(chunk)
        except BaseException as e:
            raise ExtractionError("Error trying to download an image file.", {"ImageFile object": self, "Reddit submission object": vars(self.submission)}, e)
            
        logging.info(f"Downloaded image file to {img_file}")
        self.filepath = img_file
        self.size = os.path.getsize(self.filepath)

class ExternalLink(str):
    """Subclass of str used for external links returned by Reddit."""


# === Utility functions ===

def get_media(submission: praw.reddit.Submission) -> (MediaFile | list[MediaFile] | ExternalLink | None):
    """Returns the media file(s) or link attached to a Reddit post."""
    try:
        if submission.is_gallery:
            # Submission is a gallery (contains multiple images). Collect them all
            if CONFIG["media"]["get_media"] == False:
                raise InvalidSubmissionError(f"Submission {submission.id} contains media. Media is disabled in the config. Skipping")
            files = []
            for item in submission.gallery_data["items"]:
                media_id = item["media_id"]
                # gallery_data gives the images in the correct order, but we also need to read media_metadata to get the filetype for each image so we can find them.
                filetype: str = submission.media_metadata[media_id]["m"]
                file_ext = filetype.split("/")[1]
                img = ImageFile(url=f"https://i.redd.it/{media_id}.{file_ext}")
                img.download()
                files.append(img)
            return files
    except AttributeError:
        pass # Not a gallery
    
    if not submission.thumbnail_height:
        # Submission contains no media
        if CONFIG["media"]["only_get_media"] == True:
            raise InvalidSubmissionError(f"Submission {submission.id} contains no media. The config is set to only post submissions that contain media. Skipping")
        logging.info(f"Submission {submission.id} contains no media.")
        return None
    
    if CONFIG["media"]["get_media"] == False:
        raise InvalidSubmissionError(f"Submission {submission.id} contains media. Media is disabled in the config. Skipping")
    
    # Will need to look at the url and identify what it is
    match submission.domain:
        case "i.redd.it" | "i.reddituploads.com":
            # Reddit image
            img = ImageFile(submission)
            img.download()
            return img
        case "v.redd.it":
            # Reddit video
            if CONFIG["media"]["get_videos"] == False:
                raise InvalidSubmissionError(f"Submission {submission.id} is a video. Video downloading is disabled in the config. Skipping")
            vid = VideoFile(submission)
            vid.download()
            return vid
        # Add a case for imgur later?
        case _:
            # Default/unknown case. Check to see if it's an image
            logging.info(f"Media for Reddit submission {submission.id} is from a non-Reddit domain ({submission.url}). Checking it for a compatible image")
            try:
                req = requests.get(submission.url)
                mime_type = req.headers["content-type"]
                if mime_type.startswith("image/") and get_file_ext(mime_type):
                    img = ImageFile(submission, type=mime_type)
                    img.download()
                    return img
                elif CONFIG["reddit"]["skip_link_posts"] == False:
                    logging.info("No image was found. Treating as a link post")
                    return ExternalLink(submission.url)
                else:
                    raise InvalidSubmissionError(f"No image was found at unknown URL {submission.url}. External link posts are disabled in the config. Skipping")
            except BaseException as e:
                raise ExtractionError(f"Failed to query the unknown URL {submission.url}.", {"Requests object": vars(req)}, original_error=e)
    

def get_file_ext(mime_type: str) -> str:
    """Returns a string representing the file extension (".jpg", ".mp4", etc.)
    corresponding to the given MIME type ("image/jpeg", "video/mp4", etc.)
    If the MIME type is unrecognized, an empty string is returned."""
    MIME_EXTENSIONS = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "video/mp4": ".mp4"
    }
    if mime_type in MIME_EXTENSIONS:
        return MIME_EXTENSIONS[mime_type]
    else:
        return ""
    

def add_to_cache(id: str, successes=0):
    """Records the Reddit post with the given ID to the cache file so it is not posted again.
    successes refers to the number of times the bot successfully reposted it (i.e. 1 for each Twitter account or Discord channel posted to)."""
    timestamp = time.strftime("%Y %b %d %H:%M:%S")
    with open(CACHE_FILE, "at", encoding="utf8", newline="") as file:
        file.write(f"{id},{successes},{timestamp}\n")

def check_cache(id: str, successful_only=False):
    """Returns True if the Reddit submission of the given ID is cached (i.e. the bot has already posted it or tried to post it), and False otherwise.
    If True, the bot should therefore not try to post it again.
    If successful_only=True, then True will only be returned if the bot has made at least 1 successful repost of the submission.
    """
    with open(CACHE_FILE, "r", encoding="utf8") as file:
        if successful_only:
            for line in file:
                vals = line.split(",")
                if vals[0] == id and int(vals[1]) > 0:
                    return True
        else:
            for line in file:
                vals = line.split(",", maxsplit=1)
                if vals[0] == id:
                    return True
        return False
    
def validate_submission(submission: praw.reddit.Submission):
    """Checks if a Reddit submission is one that the bot should use, based on the config and whether it's been looked at already.
    Does NOT check media.
    Returns True if the bot should proceed with this submission, False if it should skip it.
    """
    if check_cache(submission.id):
        logging.info(f"Submission {submission.id} has already been posted, skipping...")
        return False
    if CONFIG["reddit"]["skip_nsfw"] and submission.over_18:
        logging.info(f"Submission {submission.id} is NSFW, skipping...")
        return False
    if CONFIG["reddit"]["skip_stickied"] and submission.stickied:
        logging.info(f"Submission {submission.id} is stickied, skipping...")
        return False
    if CONFIG["reddit"]["skip_spoilers"] and submission.spoiler:
        logging.info(f"Submission {submission.id} is a spoiler, skipping...")
        return False
    logging.info(f"Proceeding with submission {submission.id}...")
    return True

def trim_to_limit(text: str, limit=256):
    """Trims text so that it will be within a certain character limit. Defaults to a limit of 256.
    This leaves enough room for a link to the submission within Twitter's 280 character limit.
    It is also the maximum length of the title field of a Discord embed.
    Any cut off text will be replaced with an ellipsis (…)"""
    if len(text) > limit:
        return text[:(limit-2)] + "…"
    else:
        return text

def get_tweet_text(submission: praw.reddit.Submission, url:ExternalLink=None):
    """Returns the text that will be used for a Tweet."""

    if CONFIG["twitter"]["link_in_main_tweet"]:
        if url:
            text = trim_to_limit(submission.title, 230) # URLs in Tweets are compressed down to 23 character t.co links. We need to make room for two links
            return f"{text} {url} (https://redd.it/{submission.id})"
        elif submission.selftext:
            text = trim_to_limit(f"{submission.title}\n{submission.selftext}")
            return f"{text}\nhttps://redd.it/{submission.id}"
        else:
            text = trim_to_limit(submission.title)
            return f"{text} https://redd.it/{submission.id}"  
    else:
        if url:
            text = trim_to_limit(submission.title)
            return f"{text} {url}"
        elif submission.selftext:
            return trim_to_limit(f"{submission.title}\n{submission.selftext}", 280)
        else:
            return trim_to_limit(text, 280)
    

class ThreadTweet:
    """A tweet that will be part of a multi-tweet thread. This is used for the split_tweet() function."""
    def __init__(self, index: int, first_file:MediaFile=None):
        self.index = index
        self.media = [first_file] if first_file else []
        self.media_ids = [first_file.twitter_id] if first_file else []

    def add_media(self, file: MediaFile):
        self.media.append(file)
        self.media_ids.append(file.twitter_id)

    def generate_text(self, submission: praw.reddit.Submission, thread_size: int):
        if thread_size <= 1:
            self.text = get_tweet_text(submission)
        else:
            self.text = trim_to_limit( f"({self.index + 1}/{thread_size}) {submission.title}" ) + f" https://redd.it/{submission.id}"
    
    def __len__(self):
        return len(self.media)

    def __str__(self):
        return self.text

def split_tweet(submission: praw.reddit.Submission, media: list[MediaFile]):
    """Each tweet can only contain 4 images or 1 video or 1 GIF.
    If not everything can fit onto one tweet, split them up into multiple tweets."""
    tweets: list[ThreadTweet] = [ThreadTweet(0)]
    i = 0

    for file in media:
        if isinstance(file, VideoFile) or file.type == "image/gif":
            if len(tweets[i]) >= 1:
                i += 1
                tweets.append(ThreadTweet(i, file))
            else:
                tweets[i].add_media(file)
            i += 1
            tweets.append(ThreadTweet(i))
        elif len(tweets[i]) >= 4:
            i += 1
            tweets.append(ThreadTweet(i, file))
        else:
            tweets[i].add_media(file)

    # Now generating tweet texts. We need to wait to do this because we didn't initially know the size of the tweet thread.
    for tweet in tweets:
        tweet.generate_text(submission, len(tweets))

    return tweets
