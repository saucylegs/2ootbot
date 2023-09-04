# 2ootbot
A bot that searches for posts from a specified subreddit, and reposts them to Twitter/X and Discord (more platforms coming soon?)

This bot is inspired by, and made to be a replacement for [Corbin Davenport's Tootbot](https://github.com/corbindavenport/tootbot), hence the name. However, this is a complete rewrite and not a fork of that project.

This bot has some capabilities that the original Tootbot did not, like being able to fully download and upload videos, support for Reddit Galleries, and Discord webhook support.

However, unlike the original, and despite the name, this bot does not yet support Mastodon. This was not a priority for me during development, as I do not use Mastodon; but I do want to add support sometime in the future.

## Requirements
* Some sort of computer or server to host on that lets you read and write files
* Python 3.11 or newer
* Python libraries and their dependencies (see also [requirements.txt](requirements.txt)):
  * [PRAW](https://github.com/praw-dev/praw)
  * [tweepy](https://github.com/tweepy/tweepy)
  * [discord.py](https://github.com/Rapptz/discord.py)
  * [Requests](https://github.com/psf/requests)
* [ffmpeg](https://ffmpeg.org/) installed and usable from the command line (if you want to post videos)
* A developer account for the [Reddit API](https://www.reddit.com/wiki/api/)
* A developer account for the [X API](https://developer.twitter.com) (if you want to post to TwiXer)

## Setup instructions
(work in progress)
