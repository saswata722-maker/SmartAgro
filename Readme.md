---
title: SmartAgro
emoji: 🌿
colorFrom: green
colorTo: emerald
sdk: docker
app_port: 7860
pinned: true
license: mit
short_description: AI-Powered Precision Agriculture Platform for Indian Farmers
tags:
  - agriculture
  - india
  - ai
  - flask
  - groq
  - crop-disease
  - weather
  - market-prices
  - multilingual
  - pwa
---

# 🌿 SmartAgro — AI-Powered Precision Agriculture Platform

<div align="center">

> ![SmartAgro](https://img.shields.io/badge/SmartAgro-Precision%20Agriculture-22c55e?style=for-the-badge&logo=leaf&logoColor=white)

<br/>

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![Flask](https://img.shields.io/badge/Flask-3.0-000000?style=flat-square&logo=flask&logoColor=white)](https://flask.palletsprojects.com)
[![Hugging Face Spaces](https://img.shields.io/badge/%F0%9F%A4%97%20Hugging%20Face-Spaces-blue?style=flat-square)](https://huggingface.co/spaces)
[![PWA](https://img.shields.io/badge/PWA-Installable-5A0FC8?style=flat-square&logo=pwa&logoColor=white)](https://web.dev/progressive-web-apps/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](LICENSE)
[![Made for India](https://img.shields.io/badge/Made%20for-India%20🇮🇳-FF9933?style=flat-square)](https://github.com)

**Empowering India's farmers with real-time market intelligence, AI crop diagnostics, smart weather alerts, and a multilingual voice-enabled AI assistant.**

</div>

---

## 🚀 Hugging Face Spaces — Setup

This app runs as a **Docker Space** on Hugging Face. To run it yourself, set the following **Secrets** in your Space settings:

| Secret Name | Where to Get | Free? |
|---|---|---|
| `GROQ_API_KEY` | [console.groq.com/keys](https://console.groq.com/keys) | ✅ Free |
| `OPENWEATHER_API_KEY` | [openweathermap.org/api](https://openweathermap.org/api) | ✅ Free |
| `DATA_GOV_API_KEY` | [data.gov.in](https://data.gov.in/user/register) | ✅ Free |

> **How to add secrets**: Space Settings → Variables and Secrets → New Secret

---

## ✨ Features

### 📊 Live Market Prices
- Real-time commodity prices across **20+ Indian cities**
- Price trend charts — Line, Bar, and Radar views
- Filter by demand level — Very High, High, Medium, Low
- Filter by price direction — Rising or Falling
- Live scrolling price ticker

### 🔬 AI Crop Diagnosis
- Upload or capture a photo of your crop directly from camera
- Powered by **Groq Vision AI (LLaMA 4 Scout & Maverick)**
- Identifies diseases, pests, and nutrient deficiencies with high accuracy
- Provides eco-friendly and chemical remedy plans with dosage

### 🌤️ Weather Intelligence
- Real-time weather via OpenWeatherMap
- 7-day forecast with detailed daily breakdown
- AI-powered crop recommendations based on current conditions
- Seasonal farming advisory calendar with week-by-week action plan

### 🔔 Smart Alerts
- Pest and disease risk alerts based on humidity and temperature
- Extreme weather warnings — heat, frost, storms, heavy rain
- Seasonal pest calendar showing active pests this season
- Pesticide safety guide

### 🤖 Kisan Helper — AI Voice Chatbot
- Floating voice + chat assistant available on **all pages**
- Powered by **Groq LLaMA 3.3 70B** for fast, accurate responses
- **Voice input** in your language, text auto-fills and sends
- Answers about crops, weather, market prices, government schemes

### 📱 Progressive Web App (PWA)
- **Installable on any mobile** — Android, iPhone, Windows
- Opens full-screen like a native app
- **Offline support** — cached pages work without internet

### 🌐 Multilingual Support — 23 Indian Languages
Hindi, Bengali, Telugu, Marathi, Tamil, Gujarati, Kannada, Malayalam, Punjabi, Odia, Assamese, Urdu, and more.

---

## 🛠 Tech Stack

| Layer | Technology |
|---|---|
| **Backend** | Python 3.11, Flask 3.0 |
| **Frontend** | HTML5, CSS3, Vanilla JavaScript |
| **AI Chatbot** | Groq API — LLaMA 3.3 70B Versatile |
| **AI Vision** | Groq API — LLaMA 4 Scout & Maverick |
| **Market Data** | Data.gov.in Mandi Price API |
| **Weather** | OpenWeatherMap API |
| **Charts** | Chart.js |
| **PWA** | Service Worker, Web App Manifest |
| **Voice** | Web Speech API |
| **Deployment** | Hugging Face Spaces (Docker) |

---

## 📁 Project Structure

```
SmartAgro/
│
├── app.py                    # Flask app & all API routes
├── requirements.txt          # Python dependencies
├── Dockerfile                # Docker build for HF Spaces
├── .env.example              # Template for environment variables
│
├── templates/
│   ├── index.html            # Dashboard
│   ├── diagnose.html         # AI Crop Diagnosis
│   ├── market.html           # Market Prices
│   ├── alerts.html           # Smart Alerts
│   └── offline.html          # PWA offline fallback page
│
└── static/
    ├── css/                  # Page-specific stylesheets
    ├── js/                   # Page-specific scripts + chatbot
    ├── manifest.json         # PWA manifest
    ├── service-worker.js     # PWA service worker
    └── icons/                # PWA app icons
```

---

## 🔌 API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/` | Dashboard page |
| `GET` | `/diagnose` | Crop diagnosis page |
| `GET` | `/market` | Market prices page |
| `GET` | `/alerts` | Alerts page |
| `GET` | `/api/weather?lat=&lon=` | Current weather + 7-day forecast |
| `GET` | `/api/market?location=` | Market prices with optional city filter |
| `POST` | `/api/diagnose` | AI crop disease diagnosis (vision) |
| `POST` | `/api/chat` | Kisan Helper AI chatbot (multilingual) |

---

## 🏃 Run Locally

```bash
# 1. Clone the repository
git clone https://github.com/your-username/SmartAgro.git
cd SmartAgro

# 2. Create and activate a virtual environment
python -m venv venv
source venv/bin/activate      # Linux / macOS
venv\Scripts\activate         # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set up environment variables
cp .env.example .env
# Edit .env and add your API keys

# 5. Run the app
python app.py
# Visit http://localhost:7860
```

---

## 🙏 Acknowledgements

- [Groq](https://groq.com) — blazing fast AI inference for chatbot and vision
- [OpenWeatherMap](https://openweathermap.org) — reliable weather data
- [Data.gov.in](https://data.gov.in) — mandi price data for Indian farmers
- [Chart.js](https://chartjs.org) — beautiful interactive charts
- [Font Awesome](https://fontawesome.com) — icon library
- [Google Fonts](https://fonts.google.com) — Syne & Inter typography

---

<div align="center">

**Built for India's farmers 🌾**

If this project helped you, please consider giving it a ⭐

</div>
