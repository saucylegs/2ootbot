#!/usr/bin/env python3.11

import praw
import tweepy
import discord
import requests
import logging
from datetime import datetime, timezone
from time import sleep
from util import *


def main():
    logging.info( f"({datetime.now().strftime('%d %b %Y at %H:%M:%S')}) Starting run..." )

    reddit = praw.Reddit(
        client_id=SECRETS["reddit"]["client_id"],
        client_secret=SECRETS["reddit"]["client_secret"],
        user_agent=SECRETS["reddit"]["user_agent"]
    )

    subreddit: praw.reddit.Subreddit = reddit.subreddit(CONFIG["reddit"]["subreddit"])

    # "hot", "new", "rising", "top_all", "top_hour", "top_day", "top_week", "top_month", "top_year", "random"
    match CONFIG["reddit"]["sort"]:
        case "new":
            posts = subreddit.new(limit=CONFIG["reddit"]["search_limit"])
        case "rising":
            posts = subreddit.rising(limit=CONFIG["reddit"]["search_limit"])
        case "random":
            posts = subreddit.random()
        case "top_all" | "top_hour" | "top_day" | "top_week" | "top_month" | "top_year":
            posts = subreddit.top(time_filter=CONFIG["reddit"]["sort"][4:], limit=CONFIG["reddit"]["search_limit"])
        case _:
            # Default to "hot"
            posts = subreddit.hot(limit=CONFIG["reddit"]["search_limit"])

    if not posts:
        raise TootbotError("The Reddit API did not return any posts. This is probably due to a configuration error (e.g. setting sort=random when the subreddit does not support random sorting).", severity=5)

    for submission in posts:
        if validate_submission(submission):
            try:
                media = get_media(submission)
            except TootbotError as e:
                e.log()
                continue
            # Valid submission, post it
            successful_posts = 0

            if CONFIG["twitter"]["post_to_twitter"] == True:
                try:
                    post_to_twitter(submission, media)
                    successful_posts += 1
                except BaseException as e:
                    RepostError("Error when posting to Twitter.", original_error=e).log()

            try:
                if CONFIG["discord"]["post_to_discord"] == True and len(CONFIG["discord"]["webhooks"]) > 0:
                    successful_posts += post_to_discord(submission, media)
            except BaseException as e:
                TootbotError("Critical error when trying to post to Discord.", original_error=e).log()
            
            finally:
                add_to_cache(submission.id, successful_posts)
                # Delete any media files as they are no longer needed
                if media and not isinstance(media, ExternalLink):
                    for file in media:
                        file.delete()
            
            return


def post_to_twitter(submission: praw.reddit.Submission, media: MediaFile|list[MediaFile]|ExternalLink|None):
    logging.info("Posting to Twitter...")

    twitter = tweepy.Client(
        consumer_key=SECRETS["twitter"]["consumer_key"],
        consumer_secret=SECRETS["twitter"]["consumer_secret"],
        access_token=SECRETS["twitter"]["access_token"],
        access_token_secret=SECRETS["twitter"]["access_token_secret"]
    )

    logging.info("Logged into Twitter/X.") # I would display the bot's username here, but there's a 25 requests/day rate limit on looking yourself up. I learned this the hard way. Thanks Elon

    if media == None:
        # The simplest one: if there is no media, post a text-only tweet
        tweet_text = get_tweet_text(submission)
        created = twitter.create_tweet(text=tweet_text)
        logging.info(f"Successfully tweeted submission {submission.id}. Tweet ID: {created.data['id']}")

    elif isinstance(media, ExternalLink):
        # An external link with no media. The link will be included in the Tweet
        tweet_text = get_tweet_text(submission, media)
        created = twitter.create_tweet(text=tweet_text)
        logging.info(f"Successfully tweeted submission {submission.id}. Tweet ID: {created.data['id']}")

    else:
        # We'll need to upload files to Twitter, so we also need a connection on the v1.1 API.
        v1_auth = tweepy.OAuth1UserHandler(
            SECRETS["twitter"]["consumer_key"],
            SECRETS["twitter"]["consumer_secret"],
            access_token=SECRETS["twitter"]["access_token"],
            access_token_secret=SECRETS["twitter"]["access_token_secret"]
        )
        v1 = tweepy.API(v1_auth)

        if isinstance(media, list):
            # Multiple media files; may need to tweet multiple times in a thread
            for file in media:
                file.upload_to_twitter(v1)

            tweets = split_tweet(submission, media)

            latest_tweet_id = None # The ID of the latest tweet so that we can reply to it
            for tweet in tweets:
                created = twitter.create_tweet(text=tweet.text, media_ids=tweet.media_ids, in_reply_to_tweet_id=latest_tweet_id)
                latest_tweet_id = created.data["id"]
                logging.info(f"Succesfully made tweet number {tweet.index} in thread. Tweet ID: {created.data['id']}")

        else:
            # Single media file
            tweet_text = get_tweet_text(submission)
            media.upload_to_twitter(v1)
            
            created = twitter.create_tweet(
                text = tweet_text,
                media_ids = [media.twitter_id]
            )

            logging.info(f"Successfully tweeted submission {submission.id}. Tweet ID: {created.data['id']}")


def post_to_discord(submission: praw.reddit.Submission, media: MediaFile|list[MediaFile]|ExternalLink|None):
    logging.info("Posting to Discord...")

    # Creating an embed for the discord message
    embed = discord.Embed(
        colour = discord.Colour.from_str(CONFIG["discord"]["embed_color"]),
        title = trim_to_limit( discord.utils.escape_markdown(submission.title) ),
        url = f"https://redd.it/{submission.id}",
        timestamp = datetime.fromtimestamp(submission.created_utc, timezone.utc)
    )
    if submission.selftext:
        embed.description = trim_to_limit(submission.selftext, limit=4096)
    elif isinstance(media, ExternalLink):
        embed.description = media.url
    author: praw.reddit.Redditor = submission.author
    try:
        author_icon = author.icon_img
    except AttributeError:
        author_icon = None # Suspended users do not have an icon
    embed.set_author(
        name = f"u/{author.name}",
        url = f"https://www.reddit.com/user/{author.name}",
        icon_url = author_icon
    )
    embed.set_footer(
        text = submission.subreddit_name_prefixed,
        icon_url = submission.subreddit.community_icon
    )

    # Whether or not to mark media as a spoiler
    spoiler: bool = (submission.over_18 and CONFIG["discord"]["spoiler_nsfw"]) or (submission.spoiler and CONFIG["discord"]["spoiler_spoilers"])
    successful_posts: int = 0

    with requests.Session() as session:
        for webhook_url in CONFIG["discord"]["webhooks"]:
            try:
                webhook = discord.SyncWebhook.from_url(webhook_url, session=session).fetch()
            except ValueError | discord.NotFound:
                logging.warning(f"Discord webhook {webhook_url} is invalid. Skipping")
                continue
            
            if submission.over_18 and not webhook.channel.nsfw:
                logging.info(f"Reddit submission is NSFW, but Discord channel {webhook.channel_id} is not marked as NSFW. Skipping this channel")
                continue

            if not media:
                # No media files
                try:
                    webhook.send(embed=embed, wait=True)
                except discord.HTTPException as e:
                    RepostError(f"There was an error when uploading to the Discord webhook {webhook_url}.", original_error=e, severity=3).log()
                    continue

            elif isinstance(media, ExternalLink):
                # A link post. The link will also be put outside the embed so that it can embed too.
                try:
                    webhook.send(content=media.url, embed=embed, wait=True)
                except discord.HTTPException as e:
                    RepostError(f"There was an error when uploading to the Discord webhook {webhook_url}.", original_error=e, severity=3).log()
                    continue
            
            elif isinstance(media, ImageFile):
                # One image file. Will be put inside the embed
                file = discord.File(media.filepath, spoiler=spoiler)
                embed.set_image(url=f"attachment://{file.filename}")
                try:
                    webhook.send(embed=embed, file=file, wait=True)
                except discord.HTTPException as e:
                    RepostError(f"There was an error when uploading to the Discord webhook {webhook_url}.", original_error=e, severity=3).log()
                    continue

            else:
                # One video file or multiple image files. Will need to be placed outside the embed
                files: list[discord.File] = []
                for file in media:
                    files.append( discord.File(file.filepath, spoiler=spoiler) )
                try:
                    webhook.send(embed=embed, files=files, wait=True)
                except discord.HTTPException as e:
                    RepostError(f"There was an error when uploading to the Discord webhook {webhook_url}.", original_error=e, severity=3).log()
                    continue
            
            logging.info(f"Successfully posted to Discord channel {webhook.channel_id} via webhook {webhook_url}.")
            successful_posts += 1
    
    return successful_posts

            
# Run the program
if CONFIG["behavior"]["loop"]:
    # Configured to run in an infinite loop with sleep after each run
    while True:
        try:
            main()
        except BaseException as e:
            logging.critical(e, exc_info=True) # Critical error; ensuring that it gets put into the log file
            raise e
        logging.info(f"Run complete. Sleeping for {CONFIG['behavior']['time_between_posts']} minutes...")
        sleep(CONFIG["behavior"]["time_between_posts"] * 60)

else:
    # Configured to run once
    try:
        main()
    except BaseException as e:
        logging.critical(e, exc_info=True) # Critical error; ensuring that it gets put into the log file
        raise e
    logging.info("Run complete.")
