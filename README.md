# Marketplace Automation Agent

A production-grade automation system for secure marketplace bidding with comprehensive safety controls, configurable business rules, and full audit logging.

## Features

- **Secure Authentication**: Session management with cookie persistence, automatic refresh, and challenge handling
- **Smart Scraping**: Dynamic content handling, pagination, infinite scroll support
- **Quality Filtering**: Configurable rules for seller rating, price range, condition, categories
- **Intelligent Scoring**: Multi-factor scoring engine for bid prioritization
- **Safe Bidding**: DRY_RUN simulation and LIVE modes with exposure limits
- **Budget Controls**: Daily/weekly limits, per-item caps, exposure tracking
- **Full Audit Trail**: Comprehensive logging of every decision and action
- **Alerting**: Telegram and email notifications for important events
- **CLI Interface**: Interactive configuration management

## Quick Start

### 1. Installation

```bash
# Clone the repository
git clone <repository-url>
cd Bot-scrapping

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Install Playwright browsers
playwright install chromium
```

### 2. Configuration

```bash
# Initialize default configuration
python main.py init

# Edit config/config.yaml with your marketplace settings
```

### 3. Set Credentials

Create a `.env` file in the project root:

```env
MARKETPLACE_USERNAME=your_username
MARKETPLACE_PASSWORD=your_password
```

### 4. Run in DRY_RUN Mode

```bash
# Always test with DRY_RUN first!
python main.py run --mode DRY_RUN
```

### 5. Validate Configuration

```bash
python main.py validate
```

### 6. Run in LIVE Mode (When Ready)

```bash
# Only after thorough testing!
python main.py run --mode LIVE
```

## Project Structure

```
Bot-scrapping/
├── main.py                 # Main entry point
├── config/
│   └── config.yaml         # Configuration file
├── src/
│   ├── auth/               # Authentication & session management
│   │   ├── session_manager.py
│   │   ├── credentials.py
│   │   └── challenge_handler.py
│   ├── scraper/            # Listing scraper & normalization
│   │   ├── listing_scraper.py
│   │   ├── normalizer.py
│   │   └── parser.py
│   ├── scoring/            # Quality filter & scoring engine
│   │   ├── quality_filter.py
│   │   └── scoring_engine.py
│   ├── bidding/            # Bid decision & execution
│   │   ├── bid_calculator.py
│   │   ├── dry_run_engine.py
│   │   └── live_engine.py
│   ├── config/             # Configuration management
│   │   ├── config_manager.py
│   │   └── cli_interface.py
│   ├── safety/             # Safety controls & audit logging
│   │   ├── exposure_tracker.py
│   │   ├── audit_logger.py
│   │   └── alerts.py
│   └── utils/              # Utilities
│       ├── encryption.py
│       └── helpers.py
├── data/                   # Data storage
│   ├── raw/                # Raw HTML for debugging
│   ├── processed/          # Processed listings
│   └── reports/            # DRY_RUN reports
├── logs/                   # Audit logs
├── docs/                   # Documentation
└── tests/                  # Test suite
```

## Configuration Guide

See `config/config.yaml` for all available options. Key sections:

### Marketplace Settings
Configure URLs and CSS selectors for your target marketplace.

### Budget Settings
```yaml
bidding:
  daily_budget: 100.0      # Max spend per day
  weekly_budget: 500.0     # Max spend per week
  per_item_max: 50.0       # Max bid per item
  max_daily_exposure: 200.0 # Max pending liability
```

### Filter Settings
```yaml
filter:
  min_seller_rating: 95.0
  min_price: 1.0
  max_price: 500.0
  blacklisted_categories:
    - adult
    - weapons
```

### Scoring Settings
```yaml
scoring:
  min_eligible_score: 60.0  # Minimum score to bid
  weight_seller_trust: 0.20
  weight_price_value: 0.25
```

## Safety Features

1. **DRY_RUN Mode**: Test all logic without real bids
2. **Exposure Limits**: Hard caps on pending liability
3. **Budget Tracking**: Daily/weekly spend limits
4. **Hard Stop**: Automatic stop on consecutive losses
5. **Audit Logging**: Every action logged with timestamp
6. **Human-like Delays**: Randomized timing to avoid detection

## CLI Commands

```bash
# Run the agent
python main.py run [--config PATH] [--mode DRY_RUN|LIVE] [--headless/--no-headless]

# Interactive configuration
python main.py configure [--config PATH]

# Validate configuration
python main.py validate [--config PATH]

# Initialize new config
python main.py init
```

## Documentation

- [Installation Guide](docs/installation.md)
- [Architecture Overview](docs/architecture.md)
- [Troubleshooting Guide](docs/troubleshooting.md)

## Safety Disclaimer

This tool is designed for legitimate marketplace automation. Users are responsible for:

- Complying with marketplace terms of service
- Setting appropriate budget limits
- Testing thoroughly in DRY_RUN mode
- Monitoring LIVE operations

The developers are not responsible for any financial losses or account restrictions.

## License

MIT License - See LICENSE file for details.
