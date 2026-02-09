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

import anthropic
import feedparser
import requests
from dotenv import load_dotenv

load_dotenv()

# Constants
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
DIGEST_SUBSCRIBERS = os.environ.get("DIGEST_SUBSCRIBERS", "")

CONFIG_PATH = Path(__file__).parent / "config.json"
STATE_PATH = Path(__file__).parent / "state.json"


def load_subscribers() -> dict:
    """Load subscriber emails from environment variable.

    Format: {"digest-id": ["email1@example.com", "email2@example.com"]}
    """
    if not DIGEST_SUBSCRIBERS:
        return {}
    try:
        return json.loads(DIGEST_SUBSCRIBERS)
    except json.JSONDecodeError:
        print("Warning: DIGEST_SUBSCRIBERS is not valid JSON")
        return {}


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


def _parse_duration(duration_str: str) -> tuple[int, int, int]:
    """Parse ISO 8601 duration to hours, minutes, seconds."""
    match = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration_str)
    if not match:
        return (0, 0, 0)
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    return (hours, minutes, seconds)


def _is_short_video(duration_str: str) -> bool:
    """Check if video is a Short (under 60 seconds)."""
    hours, minutes, seconds = _parse_duration(duration_str)
    total_seconds = hours * 3600 + minutes * 60 + seconds
    return total_seconds < 60


def _format_duration(duration_str: str) -> str:
    """Format ISO 8601 duration to human readable (e.g., '1:23:45' or '12:34')."""
    hours, minutes, seconds = _parse_duration(duration_str)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    else:
        return f"{minutes}:{seconds:02d}"


def _format_publish_date(published_str: str) -> str:
    """Format publish date to readable format."""
    try:
        dt = datetime.fromisoformat(published_str.replace("Z", "+00:00"))
        return dt.strftime("%b %d, %Y")
    except Exception:
        return ""


def get_video_captions(video_id: str) -> str:
    """Try to get video captions/transcript from YouTube."""
    try:
        # Try to get auto-generated captions via timedtext API
        caption_url = f"https://www.youtube.com/watch?v={video_id}"
        resp = requests.get(caption_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)

        # Look for caption track in the page
        import re as caption_re
        caption_match = caption_re.search(r'"captions":\{"playerCaptionsTracklistRenderer":\{"captionTracks":\[(\{[^\]]+\})\]', resp.text)
        if not caption_match:
            return ""

        # Extract the first caption track URL
        track_data = caption_match.group(1)
        url_match = caption_re.search(r'"baseUrl":"([^"]+)"', track_data)
        if not url_match:
            return ""

        caption_api_url = url_match.group(1).replace("\\u0026", "&")
        caption_resp = requests.get(caption_api_url, timeout=10)

        # Parse the XML caption response
        from xml.etree import ElementTree
        root = ElementTree.fromstring(caption_resp.text)
        texts = [elem.text for elem in root.findall('.//text') if elem.text]
        transcript = " ".join(texts[:200])  # Limit to first ~200 segments
        return transcript[:3000]  # Limit to 3000 chars
    except Exception:
        return ""


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


def analyze_with_claude(videos: list[dict], digest_name: str) -> dict:
    """Use Claude to analyze videos and extract insights."""
    if not ANTHROPIC_API_KEY:
        return {"themes": "AI analysis unavailable (no API key)", "video_analyses": {}}

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Prepare video summaries for analysis
    video_summaries = []
    for v in videos:
        views = v.get('views', 0)
        likes = v.get('likes', 0)
        views_str = f"{views:,}" if isinstance(views, int) else str(views)
        likes_str = f"{likes:,}" if isinstance(likes, int) else str(likes)
        summary = f"""
Video: {v['title']}
Channel: {v['channel']}
Views: {views_str}
Likes: {likes_str}
Comments count: {v.get('comment_count', 'N/A')}
Description excerpt: {v.get('description', '')[:500]}
Sample comments: {'; '.join(v.get('sample_comments', [])[:5])}
"""
        video_summaries.append(summary)

    # Get overarching themes
    themes_prompt = f"""Analyze these YouTube videos from the "{digest_name}" digest and write a brief summary identifying:

1. Overarching themes, trends, or notable patterns across the channels this week
2. Any distinctive disagreements, contrasting perspectives, or debates between channels on the same topics (if any exist)

Videos:
{"".join(video_summaries)}

Write 2-4 concise sentences. First highlight the common themes, then note any interesting disagreements or contrasting takes between channels if you notice any. Be specific about topics and which channels disagree."""

    themes = None
    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            messages=[{"role": "user", "content": themes_prompt}]
        )
        themes = response.content[0].text.strip()
    except Exception as e:
        print(f"    Theme analysis failed: {e}")
        themes = "Theme analysis unavailable"

    # Analyze each video
    video_analyses = {}
    for v in videos:
        transcript = v.get('transcript', '')
        transcript_section = ""
        if transcript:
            transcript_section = f"\nTranscript excerpt: {transcript[:2000]}"

        video_prompt = f"""Analyze this YouTube podcast/video and extract information.

Title: {v['title']}
Channel: {v['channel']}
Description: {v.get('description', '')[:1500]}
Sample comments: {'; '.join(v.get('sample_comments', [])[:10])}{transcript_section}

Extract:
1. GUESTS: Look for guest names in the title (often after "with" or before "|") and in the description. For podcasts, the title often contains the guest name. Return full names.
2. TOPICS: What are the 2-3 main topics or themes discussed? Be specific.
3. SENTIMENT: Based on the comments, is the overall reaction positive, negative, or mixed?
4. CATEGORY: Assign ONE category from: Politics, Tech, Business, Entertainment, Science, Sports, Culture, News, Education, Other
5. SUMMARY: Write a 1-2 sentence summary of what this video is about based on the title, description{' and transcript' if transcript else ''}. Be specific and informative.

You MUST respond with ONLY valid JSON in this exact format, no other text:
{{"guests": ["Full Name 1"], "topics": ["topic 1", "topic 2"], "sentiment": "positive", "category": "Politics", "summary": "A concise summary of the video content."}}

If no guests, use empty array: "guests": []"""

        parsed = None
        try:
            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=300,
                messages=[{"role": "user", "content": video_prompt}]
            )
            text = response.content[0].text.strip()
            # Find JSON in response (handle nested objects)
            json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text)
            if json_match:
                parsed = json.loads(json_match.group())
        except Exception as e:
            print(f"    Video analysis failed for {v['title']}: {e}")

        video_analyses[v['id']] = parsed or {"guests": [], "topics": [], "sentiment": "unknown", "category": "Other", "summary": ""}

    return {"themes": themes, "video_analyses": video_analyses}


def format_email_html(
    digest_name: str,
    digest_id: str,
    recipient_email: str,
    start_date: datetime,
    end_date: datetime,
    videos: list[dict],
    analysis: dict,
    repo_owner: str = "rollinrhu-cell",
    repo_name: str = "youtube-channel-digest",
) -> str:
    """Format the digest as HTML email."""
    from urllib.parse import quote

    date_range = f"{start_date.strftime('%B %d')} - {end_date.strftime('%B %d, %Y')}"

    # Build unsubscribe URL (creates a GitHub issue)
    unsubscribe_body = f"Please unsubscribe me from this digest.\\n\\n---\\nDigest ID: {digest_id}\\nEmail: {recipient_email}"
    unsubscribe_url = f"https://github.com/{repo_owner}/{repo_name}/issues/new?title=Unsubscribe&body={quote(unsubscribe_body)}&labels=unsubscribe"

    # Calculate engagement rates to identify "Must Watch" videos
    engagement_rates = []
    for v in videos:
        views = v.get("views", 0)
        likes = v.get("likes", 0)
        if views > 0:
            engagement_rates.append((v["id"], likes / views))
        else:
            engagement_rates.append((v["id"], 0))

    # Top 20% by engagement are "Must Watch"
    sorted_rates = sorted(engagement_rates, key=lambda x: x[1], reverse=True)
    must_watch_threshold = max(1, len(sorted_rates) // 5)  # Top 20%, at least 1
    must_watch_ids = set(vid_id for vid_id, _ in sorted_rates[:must_watch_threshold])

    # Group videos by category
    categories = {}
    for v in videos:
        vid_analysis = analysis.get("video_analyses", {}).get(v["id"], {})
        category = vid_analysis.get("category", "Other")
        if category not in categories:
            categories[category] = []
        categories[category].append(v)

    # Sort categories by number of videos (descending)
    sorted_categories = sorted(categories.items(), key=lambda x: len(x[1]), reverse=True)

    def format_video_card(v: dict) -> str:
        vid_analysis = analysis.get("video_analyses", {}).get(v["id"], {})
        guests = vid_analysis.get("guests", [])
        summary = vid_analysis.get("summary", "")
        sentiment = vid_analysis.get("sentiment", "unknown")

        views = v.get("views", 0)
        views_str = f"{views:,}" if views else "N/A"

        # Duration
        duration = _format_duration(v.get("duration", "PT0S"))

        # Publish date
        pub_date = _format_publish_date(v.get("published", ""))

        # Must watch badge
        must_watch_badge = ""
        if v["id"] in must_watch_ids:
            must_watch_badge = '<span style="display: inline-block; background: #fef3c7; color: #92400e; font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 4px; margin-left: 8px;">MUST WATCH</span>'

        # Build guest line
        guest_line = ""
        if guests:
            guest_line = f'<p style="margin: 0 0 4px 0; font-size: 14px;"><strong>Guest:</strong> {", ".join(guests)}</p>'

        # Build summary line
        summary_line = ""
        if summary:
            summary_line = f'<p style="margin: 8px 0; font-size: 14px; color: #444; line-height: 1.5;">{summary}</p>'

        # Build sentiment line
        sentiment_emoji = {"positive": "👍", "negative": "👎", "mixed": "🤔"}.get(sentiment, "")
        sentiment_line = ""
        if sentiment and sentiment != "unknown":
            sentiment_line = f'<span style="margin-left: 12px; color: #666;">{sentiment} {sentiment_emoji}</span>'

        # YouTube thumbnail URL
        thumbnail_url = f"https://img.youtube.com/vi/{v['id']}/mqdefault.jpg"

        return f"""<div style="margin-bottom: 24px; padding-bottom: 24px; border-bottom: 1px solid #eee;">
<a href="{v['url']}" style="display: block; margin-bottom: 12px; position: relative;">
    <img src="{thumbnail_url}" alt="{v['title']}" style="width: 100%; max-width: 320px; height: auto; border-radius: 8px;">
    <span style="position: absolute; bottom: 8px; right: 8px; background: rgba(0,0,0,0.8); color: white; font-size: 12px; padding: 2px 6px; border-radius: 4px;">{duration}</span>
</a>
<p style="margin: 0 0 4px 0; font-size: 16px; font-weight: bold;">
    <a href="{v['url']}" style="color: #1a1a1a; text-decoration: none;">{v['title']}</a>
    {must_watch_badge}
</p>
<p style="margin: 0 0 8px 0; color: #666; font-size: 13px;">
    {v['channel']} &bull; {pub_date} &bull; {views_str} views{sentiment_line}
</p>
{guest_line}
{summary_line}
</div>
"""

    # Build HTML for each category
    categories_html = ""
    for category, cat_videos in sorted_categories:
        category_icon = {
            "Politics": "🏛️",
            "Tech": "💻",
            "Business": "💼",
            "Entertainment": "🎬",
            "Science": "🔬",
            "Sports": "⚽",
            "Culture": "🎭",
            "News": "📰",
            "Education": "📚",
            "Other": "📌",
        }.get(category, "📌")

        videos_in_category = "".join(format_video_card(v) for v in cat_videos)

        categories_html += f"""
        <div style="margin-bottom: 32px;">
            <h3 style="font-size: 16px; color: #1a1a1a; margin: 0 0 16px 0; padding-bottom: 8px; border-bottom: 2px solid #e5e7eb;">
                {category_icon} {category} ({len(cat_videos)})
            </h3>
            {videos_in_category}
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

        {categories_html}

        <p style="color: #999; font-size: 12px; margin-top: 32px; text-align: center;">
            Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}<br>
            <a href="{unsubscribe_url}" style="color: #999;">Unsubscribe</a>
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

    # Add plain text version first (fallback)
    # Strip HTML tags for plain text
    import re as re_email
    plain_text = re_email.sub(r'<[^>]+>', '', html_content)
    plain_text = re_email.sub(r'\s+', ' ', plain_text).strip()
    msg.attach(MIMEText(plain_text, "plain"))

    # Add HTML version
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


def run_digest(digest: dict, state: dict, subscribers: dict) -> bool:
    """Run a single digest."""
    name = digest["name"]
    digest_id = digest.get("id", name)
    # Get recipients from secret (priority) or fall back to config
    recipients = subscribers.get(digest_id, digest.get("recipients", []))
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

    # Filter out YouTube Shorts (videos under 60 seconds OR with /shorts/ in URL)
    all_videos = [v for v in all_videos if not v.get("is_short", False) and "/shorts/" not in v.get("url", "")]

    # Fetch transcripts for remaining videos (for better summaries)
    print("  Fetching transcripts...")
    for v in all_videos:
        v["transcript"] = get_video_captions(v["id"])

    if not all_videos:
        print(f"  No new videos (excluding Shorts) found for {name}")
        return False

    print(f"  {len(all_videos)} videos after filtering Shorts")

    # Analyze with Claude
    print("  Analyzing with Claude...")
    analysis = analyze_with_claude(all_videos, name)

    # Format and send email to each recipient (personalized unsubscribe links)
    subject = f"{name} - {start_date.strftime('%B %d')} to {end_date.strftime('%B %d, %Y')}"
    success = False
    for recipient in recipients:
        html = format_email_html(
            digest_name=name,
            digest_id=digest_id,
            recipient_email=recipient,
            start_date=start_date,
            end_date=end_date,
            videos=all_videos,
            analysis=analysis,
        )
        if send_email([recipient], subject, html):
            success = True

    # Update state
    if success:
        state[digest_id] = {
            "last_run": datetime.now().isoformat(),
            "seen_ids": list(seen_ids | set(video_ids))[-500:],  # Keep last 500
        }

    return success


def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="YouTube Channel Email Digest")
    parser.add_argument("--digest", "-d", help="Run only this specific digest (by ID or name)")
    args = parser.parse_args()

    config = load_config()
    state = load_state()
    subscribers = load_subscribers()

    if not config.get("digests"):
        print("No digests configured. Add digests via the web interface or config.json")
        return

    for digest in config["digests"]:
        digest_id = digest.get("id", digest["name"])

        # If a specific digest was requested, skip others
        if args.digest and args.digest != digest_id and args.digest != digest["name"]:
            continue

        # Get recipients from secret (priority) or config
        recipients = subscribers.get(digest_id, digest.get("recipients", []))

        # Skip digests with no recipients
        if not recipients:
            print(f"Skipping {digest['name']} (no recipients)")
            continue

        # When running a specific digest, always run it (ignore schedule)
        if args.digest or should_run_digest(digest, state):
            run_digest(digest, state, subscribers)
        else:
            print(f"Skipping {digest['name']} (not scheduled)")

    save_state(state)


if __name__ == "__main__":
    main()
