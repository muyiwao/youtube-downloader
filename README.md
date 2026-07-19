# Video & Audio Downloader Web App

A Streamlit interface around `yt-dlp` for downloading content that you own, that is in the public domain, or that you have permission to use.

## Features

- Up to five URLs per run
- Full video or a shared time range
- Video with audio, silent video, MP3 audio, or JPG thumbnail
- Concurrent downloads
- ZIP download from the browser
- Request-specific temporary storage and automatic cleanup

## Responsible use

Do not use this project to infringe copyright or bypass authentication, DRM, paywalls, geographic restrictions, or other access controls. Website terms and applicable laws still apply.

## Run locally

1. Install Python 3.11 or later.
2. Install FFmpeg and Node.js, and ensure both are available on `PATH`.
3. Create and activate a virtual environment.
4. Install dependencies and start Streamlit:

```bash
python -m venv .venv

# Windows PowerShell
.venv\Scripts\Activate.ps1

# macOS/Linux
source .venv/bin/activate

python -m pip install --upgrade pip
pip install -r requirements.txt
streamlit run app.py
```

## Repository layout

```text
youtube-downloader-streamlit/
├── .streamlit/
│   └── config.toml
├── app.py
├── downloader.py
├── packages.txt
├── requirements.txt
├── .gitignore
└── README.md
```

## Deploy to Streamlit Community Cloud

1. Push this folder to a GitHub repository.
2. Sign in to Streamlit Community Cloud with GitHub.
3. Create a new app.
4. Select the repository, `main` branch, and `app.py` as the entrypoint.
5. Deploy and test with a short video that you own.

## Important hosting limitation

A successful local test does not guarantee that every media site will work from Community Cloud. Sites can block cloud-hosting IP addresses, require cookies, alter extraction logic, or impose rate limits. Free Streamlit resources are also unsuitable for long or highly concurrent downloads.
