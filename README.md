# Telegram AI Assistant Bot 

A production-oriented **Telegram AI assistant bot** designed as an academic-level software project.
The bot provides AI-powered text understanding, document analysis, image recognition, and image generation, while implementing a **subscription + trial access model without using a database**.

The project focuses on clean architecture, real-world logic, and lightweight deployment, making it suitable for both practical use and academic evaluation.

---

## Core Functionality

### AI Capabilities

* Answers **text questions** in the same language as the user
* Analyzes **web links** and solves tests or quizzes found on pages (including Google Forms)
* Processes **PDF documents** (multi-page, text-based)
* Processes **TXT files** and answers all questions concisely
* Understands **images of tasks or screenshots** and provides solutions
* Generates **AI images from text prompts** using a limited key-based system

AI text and image understanding is powered by **Google Gemini**, while image generation is handled through **Replicate** with automatic API key rotation.

---

## Access Model

The bot simulates a real production access system.

### Subscription

* Time-based subscription (days)
* Manual approval via **receipt image verification**
* Image generation keys are granted on activation

### Trial Access

* Limited duration (time-based)
* Cooldown between trials
* Only **one image generation** allowed during trial

This logic closely reflects real monetized Telegram bots.

---

## Language Support

The bot supports a multilingual interface:

* English
* Russian
* Azerbaijani

Users can change the interface language at any time.
AI responses automatically follow the user’s language.

---

## Commands

### User Commands

* `/start` — Start interaction
* `/help` — Usage instructions
* `/subscribe` — Subscription instructions
* `/status` — Check access status
* `/trial` — Activate trial access
* `/profile` — View profile and image keys
* `/language` — Change interface language

### Admin Commands

* `/subscribers` — View active subscribers
* `/requests` — View pending subscription requests
* `/givesub <user_id> <days>` — Grant subscription
* `/trialgive <user_id>` — Grant trial access
* `/announce` — Send announcements to subscribers

---

## Image Handling Logic

When a user sends an image:

* If access is active → the image is processed as a **task**
* If access is inactive → the user chooses:

  * 📝 Task
  * 🧾 Payment receipt

This cleanly separates learning functionality from payment moderation.

---

## Document Processing

### PDF

* Multi-page support
* Chunk-based processing
* Handles large documents safely
* Detects non-text PDFs (scans)

### TXT

* Reads full content
* Answers all questions concisely
* Preserves original language

---

## Storage Architecture (No Database)

The project intentionally avoids databases to keep deployment simple.

### Used storage methods

* **JSON**

  * Active subscribers
  * Expiration timestamps
* **Pickle**

  * Trial data
  * Image generation keys
  * Pending payment requests
  * Language preferences

All data is persistent across restarts.

---

## Tech Stack

* **Language:** Python 3.10+
* **Telegram Framework:** pyTelegramBotAPI (telebot)
* **AI Models:** Google Gemini (text + image understanding)
* **Image Generation:** Replicate
* **Web Parsing:** BeautifulSoup
* **PDF Processing:** PyPDF2
* **Image Handling:** Pillow
* **Web Server:** Flask (keep-alive)
* **Hosting:** Render (compatible)

---

## Security & Configuration

* All secrets are stored in **environment variables**
* No API keys or tokens are committed to the repository
* Admin-only actions are protected by ID checks
* API key rotation is implemented for fault tolerance

### Required environment variables

* `BOT_TOKEN`
* `ADMIN_ID`
* `GEMINI_API_KEYS`
* `REPLICATE_API_KEYS`
* `SELF_PING_URL` (optional)

---

## Deployment

* Designed for cloud platforms such as **Render**
* Includes a lightweight Flask server for health checks
* Optional keep-alive self-ping to prevent free-tier sleep
* Automatic restart loop for stability

The bot runs continuously and recovers gracefully from errors.

---

## Project Purpose

This project demonstrates:

* Practical Telegram bot development
* AI integration in real applications
* Subscription and trial logic without databases
* File-based persistence strategies
* Multilingual UX design
* Cloud deployment and uptime management
* Clean, production-oriented Python architecture

The implementation is concise, realistic, and suitable for **academic review or portfolio use**.

---

## Live Bot
[Telegram Bot](https://t.me/GetaiAnswers_bot)
