# YouTube Channel Email Digest

Monitors YouTube channels and sends email digests with AI-generated summaries of new uploads.

## Features

- **Channel monitoring** — Track any YouTube channels via RSS feeds
- **AI analysis** — Gemini extracts guests, topics, and comment sentiment
- **Theme detection** — Identifies overarching themes across channels
- **Flexible scheduling** — Daily, twice-weekly, or weekly digests
- **Cloud-based** — Runs automatically via GitHub Actions at 7am ET

## Sample Email

```
Tech Podcasts Weekly
January 27 - February 3, 2026

THIS WEEK'S THEMES
This week saw a focus on AI regulation debates and the economic implications
of automation, with multiple guests discussing workforce transitions...

NEW UPLOADS (5)

The Future of Work with Daron Acemoglu
Ezra Klein Show • 245,000 views
Guests: Daron Acemoglu
Topics: AI and labor markets, automation anxiety, policy responses
Comment sentiment: mixed 🤔
```

## Setup

### 1. Fork or clone this repo

### 2. Configure your digest

Use the [web configurator](https://rollinrhu-cell.github.io/digest-config.html) or manually edit `config.json`:

```json
{
  "digests": [
    {
      "id": "tech-podcasts",
      "name": "Tech Podcasts Weekly",
      "recipients": ["you@email.com"],
      "channels": [
        "https://www.youtube.com/@ezaborowski",
        "https://www.youtube.com/@lexfridman"
      ],
      "frequency": "weekly"
    }
  ]
}
```

### 3. Add GitHub Secrets

Go to Settings → Secrets → Actions and add:

| Secret | Description |
|--------|-------------|
| `YOUTUBE_API_KEY` | [Google Cloud Console](https://console.cloud.google.com/apis/credentials) - Enable YouTube Data API v3 |
| `GEMINI_API_KEY` | Your Gemini API key |
| `GMAIL_ADDRESS` | Gmail address to send from |
| `GMAIL_APP_PASSWORD` | [Generate App Password](https://myaccount.google.com/apppasswords) (requires 2FA) |

### 4. Enable GitHub Actions

The workflow runs automatically at 7am ET daily. You can also trigger manually:
Actions → YouTube Digest → Run workflow

## Configuration Options

| Field | Description |
|-------|-------------|
| `id` | Unique identifier (auto-generated from name) |
| `name` | Display name for the digest |
| `recipients` | Array of email addresses |
| `channels` | Array of YouTube channel URLs |
| `frequency` | `daily`, `biweekly`, or `weekly` |

## Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Create .env file
cp .env.example .env
# Edit .env with your API keys

# Run manually
python digest.py
```

## License

MIT
