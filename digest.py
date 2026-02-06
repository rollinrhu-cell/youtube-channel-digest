#!/usr/bin/env python3
"""YouTube Channel Email Digest - Monitors channels and sends summary emails."""

from __future__ import annotations

import json
import os
import re
import smtplib
import sys
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

import feedparser
import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

# Constants
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")

CONFIG_PATH = Path(__file__).parent / "config.json"
STATE_PATH = Path(__file__).parent / "state.json"


def load_config() -> dict:
    """Load digest configuration."""
    if not CONFIG_PATH.exists():
        return {"digests": []}
    return json.loads(CONFIG_PATH.read_text())


def load_state() -> dict:
    """Load state (last check times, seen video IDs)."""
    if not STATE_PATH.exists():
        return {}
    return json.loads(STATE_PATH.read_text())


def save_state(state: dict):
    """Save state."""
    STATE_PATH.write_text(json.dumps(state, indent=2))


def extract_channel_id(url: str) -> str | None:
    """Extract channel ID from various YouTube URL formats."""
    # Direct channel ID
    if match := re.search(r"youtube\.com/channel/([a-zA-Z0-9_-]+)", url):
        return match.group(1)

    # Handle/@username format - need to resolve via page or API
    if match := re.search(r"youtube\.com/@([a-zA-Z0-9_-]+)", url):
        username = match.group(1)
        return resolve_username_to_channel_id(username)

    # /c/ or /user/ format
    if match := re.search(r"youtube\.com/(?:c|user)/([a-zA-Z0-9_-]+)", url):
        username = match.group(1)
        return resolve_username_to_channel_id(username)

    return None


def resolve_username_to_channel_id(username: str) -> str | None:
    """Resolve a YouTube username/handle to channel ID."""
    # Try fetching the channel page and extracting the ID
    try:
        resp = requests.get(
            f"https://www.youtube.com/@{username}",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        if match := re.search(r'"channelId":"([a-zA-Z0-9_-]+)"', resp.text):
            return match.group(1)
    except Exception:
        pass
    return None


def get_channel_feed(channel_id: str) -> list[dict]:
    """Get recent videos from a channel's RSS feed."""
    feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    feed = feedparser.parse(feed_url)

    videos = []
    for entry in feed.entries:
        videos.append({
            "id": entry.yt_videoid,
            "title": entry.title,
            "url": entry.link,
            "published": entry.published,
            "channel": feed.feed.get("title", "Unknown"),
            "channel_id": channel_id,
        })
    return videos


def _is_short_video(duration_str: str) -> bool:
    """Check if video is a Short (under 60 seconds). Duration is ISO 8601 format like PT1M30S."""
    import re
    # Parse ISO 8601 duration (e.g., PT1H2M30S, PT5M, PT30S)
    match = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration_str)
    if not match:
        return False
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    total_seconds = hours * 3600 + minutes * 60 + seconds
    return total_seconds < 60


def get_video_details(video_ids: list[str]) -> dict[str, dict]:
    """Get detailed video info via YouTube Data API."""
    if not YOUTUBE_API_KEY or not video_ids:
        return {}

    details = {}
    # API allows up to 50 videos per request
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i+50]
        url = "https://www.googleapis.com/youtube/v3/videos"
        params = {
            "key": YOUTUBE_API_KEY,
            "id": ",".join(batch),
            "part": "snippet,statistics,contentDetails",
        }
        try:
            resp = requests.get(url, params=params, timeout=15)
            data = resp.json()
            for item in data.get("items", []):
                vid = item["id"]
                stats = item.get("statistics", {})
                snippet = item.get("snippet", {})
                content = item.get("contentDetails", {})

                # Parse duration to check for Shorts (under 60 seconds)
                duration_str = content.get("duration", "PT0S")  # ISO 8601 format
                is_short = _is_short_video(duration_str)

                details[vid] = {
                    "views": int(stats.get("viewCount", 0)),
                    "likes": int(stats.get("likeCount", 0)),
                    "comments": int(stats.get("commentCount", 0)),
                    "description": snippet.get("description", ""),
                    "tags": snippet.get("tags", []),
                    "is_short": is_short,
                    "duration": duration_str,
                }
        except Exception as e:
            print(f"Error fetching video details: {e}")

    return details


def get_video_comments(video_id: str, max_comments: int = 50) -> list[str]:
    """Get top comments for a video."""
    if not YOUTUBE_API_KEY:
        return []

    url = "https://www.googleapis.com/youtube/v3/commentThreads"
    params = {
        "key": YOUTUBE_API_KEY,
        "videoId": video_id,
        "part": "snippet",
        "order": "relevance",
        "maxResults": max_comments,
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()
        comments = []
        for item in data.get("items", []):
            text = item["snippet"]["topLevelComment"]["snippet"]["textDisplay"]
            comments.append(text)
        return comments
    except Exception:
        return []


def analyze_with_gemini(videos: list[dict], digest_name: str) -> dict:
    """Use Gemini to analyze videos and extract insights."""
    if not GEMINI_API_KEY:
        return {"themes": "AI analysis unavailable (no API key)", "video_analyses": {}}

    client = genai.Client(api_key=GEMINI_API_KEY)

    # Prepare video summaries for analysis
    video_summaries = []
    for v in videos:
        summary = f"""
Video: {v['title']}
Channel: {v['channel']}
Views: {v.get('views', 'N/A'):,}
Likes: {v.get('likes', 'N/A'):,}
Comments count: {v.get('comment_count', 'N/A')}
Description excerpt: {v.get('description', '')[:500]}
Sample comments: {'; '.join(v.get('sample_comments', [])[:5])}
"""
        video_summaries.append(summary)

    # Get overarching themes
    themes_prompt = f"""Analyze these YouTube videos from the "{digest_name}" digest and write a brief paragraph (2-3 sentences) identifying overarching themes, trends, or notable patterns across the channels this week.

Videos:
{"".join(video_summaries)}

Write a concise, insightful paragraph about the themes. Be specific about topics discussed."""

    try:
        themes_response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=themes_prompt,
            config=types.GenerateContentConfig(temperature=0.3),
        )
        themes = themes_response.text.strip()
    except Exception as e:
        themes = f"Unable to generate themes: {e}"

    # Analyze each video
    video_analyses = {}
    for v in videos:
        video_prompt = f"""Analyze this YouTube podcast/video and extract information.

Title: {v['title']}
Channel: {v['channel']}
Description: {v.get('description', '')[:1500]}
Sample comments: {'; '.join(v.get('sample_comments', [])[:10])}

Extract:
1. GUESTS: Look for guest names in the title (often after "with" or before "|") and in the description. For podcasts, the title often contains the guest name. Return full names.
2. TOPICS: What are the 2-3 main topics or themes discussed? Be specific.
3. SENTIMENT: Based on the comments, is the overall reaction positive, negative, or mixed?

You MUST respond with ONLY valid JSON in this exact format, no other text:
{{"guests": ["Full Name 1", "Full Name 2"], "topics": ["specific topic 1", "specific topic 2"], "sentiment": "positive"}}

If no guests, use empty array: "guests": []"""

        try:
            response = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=video_prompt,
                config=types.GenerateContentConfig(temperature=0.1),
            )
            # Extract JSON from response
            text = response.text.strip()
            # Find JSON in response
            json_match = re.search(r'\{[^{}]+\}', text)
            if json_match:
                video_analyses[v['id']] = json.loads(json_match.group())
            else:
                video_analyses[v['id']] = {"guests": [], "topics": [], "sentiment": "unknown"}
        except Exception:
            video_analyses[v['id']] = {"guests": [], "topics": [], "sentiment": "unknown"}

    return {"themes": themes, "video_analyses": video_analyses}


def format_email_html(
    digest_name: str,
    start_date: datetime,
    end_date: datetime,
    videos: list[dict],
    analysis: dict,
) -> str:
    """Format the digest as HTML email."""

    date_range = f"{start_date.strftime('%B %d')} - {end_date.strftime('%B %d, %Y')}"

    videos_html = ""
    for v in videos:
        vid_analysis = analysis.get("video_analyses", {}).get(v["id"], {})
        guests = vid_analysis.get("guests", [])
        topics = vid_analysis.get("topics", [])
        sentiment = vid_analysis.get("sentiment", "unknown")

        guest_str = f"<strong>Guests:</strong> {', '.join(guests)}<br>" if guests else ""
        topics_str = "<strong>Topics:</strong> " + ", ".join(topics) + "<br>" if topics else ""

        sentiment_emoji = {"positive": "👍", "negative": "👎", "mixed": "🤔"}.get(sentiment, "")
        sentiment_str = f"<strong>Comment sentiment:</strong> {sentiment} {sentiment_emoji}<br>" if sentiment != "unknown" else ""

        views = v.get("views", 0)
        views_str = f"{views:,}" if views else "N/A"

        videos_html += f"""
        <div style="margin-bottom: 24px; padding-bottom: 24px; border-bottom: 1px solid #e0e0e0;">
            <h3 style="margin: 0 0 8px 0; font-size: 16px;">
                <a href="{v['url']}" style="color: #1a1a1a; text-decoration: none;">{v['title']}</a>
            </h3>
            <p style="margin: 0 0 8px 0; color: #666; font-size: 14px;">
                {v['channel']} • {views_str} views
            </p>
            <p style="margin: 0; font-size: 14px; color: #333;">
                {guest_str}
                {topics_str}
                {sentiment_str}
            </p>
        </div>
        """

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
    </head>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; color: #1a1a1a;">
        <h1 style="font-size: 24px; margin-bottom: 4px;">{digest_name}</h1>
        <p style="color: #666; margin-top: 0; margin-bottom: 24px;">{date_range}</p>

        <div style="background: #f8f9fa; padding: 16px; border-radius: 8px; margin-bottom: 32px;">
            <h2 style="font-size: 14px; text-transform: uppercase; letter-spacing: 1px; color: #666; margin: 0 0 8px 0;">This Week's Themes</h2>
            <p style="margin: 0; font-size: 15px; line-height: 1.5;">{analysis.get('themes', 'No themes available.')}</p>
        </div>

        <h2 style="font-size: 14px; text-transform: uppercase; letter-spacing: 1px; color: #666; margin-bottom: 16px;">New Uploads ({len(videos)})</h2>

        {videos_html}

        <p style="color: #999; font-size: 12px; margin-top: 32px; text-align: center;">
            Generated by YouTube Channel Digest
        </p>
    </body>
    </html>
    """
    return html


def send_email(recipients: list[str], subject: str, html_content: str):
    """Send email via Gmail SMTP."""
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        print("Gmail credentials not configured. Email not sent.")
        print(f"Would send to: {recipients}")
        print(f"Subject: {subject}")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = ", ".join(recipients)

    msg.attach(MIMEText(html_content, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, recipients, msg.as_string())
        print(f"Email sent to {recipients}")
        return True
    except Exception as e:
        print(f"Failed to send email: {e}")
        return False


def should_run_digest(digest: dict, state: dict) -> bool:
    """Check if a digest should run based on frequency."""
    digest_id = digest.get("id", digest["name"])
    last_run = state.get(digest_id, {}).get("last_run")

    if not last_run:
        return True

    last_run_dt = datetime.fromisoformat(last_run)
    now = datetime.now()
    frequency = digest.get("frequency", "daily")

    if frequency == "daily":
        return (now - last_run_dt).days >= 1
    elif frequency == "biweekly":
        return (now - last_run_dt).days >= 3  # Twice a week ~ every 3-4 days
    elif frequency == "weekly":
        return (now - last_run_dt).days >= 7

    return True


def run_digest(digest: dict, state: dict) -> bool:
    """Run a single digest."""
    name = digest["name"]
    recipients = digest["recipients"]
    channels = digest["channels"]
    frequency = digest.get("frequency", "daily")

    print(f"\nProcessing digest: {name}")

    # Determine date range
    end_date = datetime.now()
    if frequency == "daily":
        start_date = end_date - timedelta(days=1)
    elif frequency == "biweekly":
        start_date = end_date - timedelta(days=3)
    else:  # weekly
        start_date = end_date - timedelta(days=7)

    # Get videos from all channels
    all_videos = []
    digest_id = digest.get("id", name)
    seen_ids = set(state.get(digest_id, {}).get("seen_ids", []))

    for channel_url in channels:
        channel_id = extract_channel_id(channel_url)
        if not channel_id:
            print(f"  Could not extract channel ID from: {channel_url}")
            continue

        print(f"  Fetching: {channel_url} ({channel_id})")
        videos = get_channel_feed(channel_id)

        for v in videos:
            pub_date = datetime.fromisoformat(v["published"].replace("Z", "+00:00"))
            pub_date = pub_date.replace(tzinfo=None)

            # Only include videos in date range and not seen
            if pub_date >= start_date and v["id"] not in seen_ids:
                all_videos.append(v)

    if not all_videos:
        print(f"  No new videos found for {name}")
        return False

    print(f"  Found {len(all_videos)} new videos")

    # Get detailed info for videos
    video_ids = [v["id"] for v in all_videos]
    details = get_video_details(video_ids)

    # Enrich videos with details and comments
    for v in all_videos:
        if v["id"] in details:
            v.update(details[v["id"]])
            v["comment_count"] = details[v["id"]].get("comments", 0)
        v["sample_comments"] = get_video_comments(v["id"], max_comments=20)

    # Filter out YouTube Shorts (videos under 60 seconds)
    all_videos = [v for v in all_videos if not v.get("is_short", False)]

    if not all_videos:
        print(f"  No new videos (excluding Shorts) found for {name}")
        return False

    print(f"  {len(all_videos)} videos after filtering Shorts")

    # Analyze with Gemini
    print("  Analyzing with Gemini...")
    analysis = analyze_with_gemini(all_videos, name)

    # Format and send email
    html = format_email_html(name, start_date, end_date, all_videos, analysis)
    subject = f"{name} - {end_date.strftime('%B %d, %Y')}"

    success = send_email(recipients, subject, html)

    # Update state
    if success:
        state[digest_id] = {
            "last_run": datetime.now().isoformat(),
            "seen_ids": list(seen_ids | set(video_ids))[-500:],  # Keep last 500
        }

    return success


def main():
    """Main entry point."""
    config = load_config()
    state = load_state()

    if not config.get("digests"):
        print("No digests configured. Add digests via the web interface or config.json")
        return

    for digest in config["digests"]:
        if should_run_digest(digest, state):
            run_digest(digest, state)
        else:
            print(f"Skipping {digest['name']} (not scheduled)")

    save_state(state)


if __name__ == "__main__":
    main()
