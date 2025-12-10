# Installation Guide

This guide walks you through installing and setting up the Marketplace Automation Agent from scratch.

## Prerequisites

- Python 3.10 or higher
- pip (Python package manager)
- Git
- 4GB+ RAM recommended
- Stable internet connection

## Step-by-Step Installation

### 1. Clone the Repository

```bash
git clone <repository-url>
cd Bot-scrapping
```

### 2. Create Virtual Environment

It's recommended to use a virtual environment to avoid conflicts with other Python projects.

**Linux/macOS:**
```bash
python3 -m venv venv
source venv/bin/activate
```

**Windows:**
```bash
python -m venv venv
venv\Scripts\activate
```

### 3. Install Python Dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

This installs:
- Playwright (browser automation)
- BeautifulSoup (HTML parsing)
- Pydantic (configuration validation)
- Rich (CLI interface)
- And other dependencies

### 4. Install Playwright Browsers

Playwright needs browser binaries. Install Chromium:

```bash
playwright install chromium
```

For full browser support (optional):
```bash
playwright install
```

### 5. Initialize Configuration

```bash
python main.py init
```

This creates `config/config.yaml` with default settings.

### 6. Configure Credentials

Create a `.env` file in the project root:

```env
# Required credentials
MARKETPLACE_USERNAME=your_username
MARKETPLACE_PASSWORD=your_password

# Optional: For 2FA
MARKETPLACE_TOTP_SECRET=your_totp_secret

# Optional: API key if supported
MARKETPLACE_API_KEY=your_api_key
```

**Security Note:** Never commit the `.env` file to version control.

### 7. Customize Configuration

Edit `config/config.yaml` for your marketplace:

1. **Update marketplace URLs:**
```yaml
marketplace:
  name: my_marketplace
  base_url: https://actual-marketplace.com
  listings_url: https://actual-marketplace.com/listings
```

2. **Customize CSS selectors** (inspect your marketplace's HTML):
```yaml
  login_selectors:
    username_input: '#email'
    password_input: '#password'
    submit_button: '#login-btn'
```

3. **Set budget limits:**
```yaml
bidding:
  daily_budget: 100.0
  per_item_max: 50.0
```

### 8. Verify Installation

```bash
# Check configuration is valid
python main.py validate

# Run in DRY_RUN mode to test
python main.py run --mode DRY_RUN
```

## Directory Setup

The agent creates these directories automatically:

```
data/
├── raw/        # Raw HTML pages (for debugging)
├── processed/  # Processed listings JSON
├── reports/    # DRY_RUN simulation reports
└── sessions/   # Session cookies

logs/           # Audit logs
```

## Troubleshooting Installation

### "playwright: command not found"

Ensure you activated the virtual environment:
```bash
source venv/bin/activate  # Linux/macOS
venv\Scripts\activate     # Windows
```

### "ModuleNotFoundError"

Reinstall dependencies:
```bash
pip install -r requirements.txt
```

### "Browser not found"

Install Playwright browsers:
```bash
playwright install chromium
```

### Permission errors on Linux

You may need to install system dependencies:
```bash
sudo apt-get update
sudo apt-get install -y libgbm1 libasound2
```

## Next Steps

1. Read the [Configuration Guide](../config/config.yaml) for all options
2. Study [Architecture Overview](architecture.md) to understand the system
3. Run initial tests in DRY_RUN mode
4. Monitor logs in the `logs/` directory

## Updating

To update the agent:

```bash
git pull origin main
pip install -r requirements.txt --upgrade
```

Check the changelog for any configuration changes needed.
