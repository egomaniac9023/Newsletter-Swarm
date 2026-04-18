# Newsletter Swarm

A fast and powerful parallel Python tool for mass newsletter subscriptions using **Mullvad SOCKS5 proxies only**.

Built for testing, research, and development purposes.

---

## ✨ Features

- High-performance parallel execution (up to 230 concurrent workers)
- Support for multiple services simultaneously
- Repeat submissions per email with `--repeats`
- Smart **LazyResolver** – resolves and tests only as many proxies as needed
- Automatic fetching of fresh Mullvad SOCKS5 proxies via Mullvad API
- Robust cookie and security token handling
- Flexible JSON configuration per service
- Success/failure detection via markers or URL patterns
- Detailed console logging

---

## 🚀 Quick Start

### Run a single service

```bash
python test.py --service mediashop --email your@email.com --repeats 10 --refresh-proxies --match-jobs
```

### Run all services at once

```bash
python test.py --all-services --email your@email.com --repeats 30 --refresh-proxies --match-jobs
```

---

## 📋 Requirements

```bash
pip install requests dnspython
```

---

## 🔑 Mullvad VPN Requirement (Important!)

This tool **only supports Mullvad SOCKS5 proxies**.

- It automatically fetches the current list of Mullvad SOCKS5 proxies when using `--refresh-proxies`.
- **You must be connected to the Mullvad VPN** for the proxies to work correctly.
- No other proxy providers are supported.

---

## 📁 Service Configurations

Each service is defined in its own JSON configuration file placed in the project root.

Create one `.json` file per service (e.g. `myservice.json`).  
The script automatically detects and loads all `.json` files when using `--all-services`.

Example structure:

```json
{
    "cookie_url": "https://example.com/newsletter",
    "post_url": "https://example.com/api/subscribe",
    "payload": {
        "email": "{email}"
    },
    "headers": {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0",
        "Content-Type": "application/json"
    }
}
```

The placeholder `{email}` is automatically replaced with the target email address.

---

## 🔧 Command Line Arguments

| Argument                | Description |
|------------------------|-------------|
| `--service <name>`     | Run one or more specific services (can be used multiple times) |
| `--all-services`       | Use all `.json` files in the folder |
| `--email <address>`    | Email address (can be used multiple times) |
| `--file emails.txt`    | Load emails from a text file (one per line) |
| `--repeats N`          | How many times each email should be submitted per service |
| `--refresh-proxies`    | Fetch fresh Mullvad SOCKS5 proxies from the API |
| `--match-jobs`         | Automatically resolve as many proxies as total jobs (recommended) |
| `--need N`             | Manually define how many proxies to resolve |

**Recommended usage:**
```bash
--refresh-proxies --match-jobs
```

---

## 🛠️ Technical Details

- **Mullvad SOCKS5 only** via `socks5://`
- Multithreaded LazyResolver with DNS resolution and connectivity testing
- Supports both `application/json` and `application/x-www-form-urlencoded`
- Automatic `securityToken` extraction from HTML responses
- Uses `ThreadPoolExecutor` for high concurrency

---

## 📂 Project Structure

```
mullvad-newsletter-bomber/
├── test.py
├── your-service.json
├── another-service.json
├── emails.txt (optional)
└── README.md
```

---

## ⚠️ Important Notes

- This tool is intended **for testing and research purposes only**.
- Always respect the terms of service of the websites you target.
- Misuse for spam or unsolicited mass subscriptions is strictly prohibited.
- Services protected by strong CAPTCHA or advanced bot detection may not work reliably.

---

**For educational and testing purposes only.**
