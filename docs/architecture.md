# Architecture Overview

This document describes the system architecture of the Marketplace Automation Agent.

## System Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           MARKETPLACE AUTOMATION AGENT                       │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐                   │
│  │   Config     │    │   Safety     │    │   Audit      │                   │
│  │   Manager    │    │   Controls   │    │   Logger     │                   │
│  └──────────────┘    └──────────────┘    └──────────────┘                   │
│         │                   │                   │                            │
│  ───────┴───────────────────┴───────────────────┴─────────────────────────  │
│                                                                              │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │                         MAIN ORCHESTRATOR                             │   │
│  │  (Coordinates the scrape → filter → score → bid pipeline)            │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│         │                                                                    │
│  ───────┴─────────────────────────────────────────────────────────────────  │
│         │                                                                    │
│  ┌──────┴──────┐    ┌──────────────┐    ┌──────────────┐    ┌────────────┐  │
│  │   Auth &    │───▶│   Listing    │───▶│   Quality    │───▶│  Scoring   │  │
│  │   Session   │    │   Scraper    │    │   Filter     │    │  Engine    │  │
│  └─────────────┘    └──────────────┘    └──────────────┘    └────────────┘  │
│                                                                    │         │
│  ───────────────────────────────────────────────────────────────────┴─────  │
│                                                                    │         │
│  ┌──────────────────────────────────────────────────────────────────┴────┐  │
│  │                         BID DECISION ENGINE                           │  │
│  │  ┌────────────────┐    ┌────────────────┐    ┌────────────────┐       │  │
│  │  │ Bid Calculator │───▶│ DRY_RUN Engine │    │  LIVE Engine   │       │  │
│  │  └────────────────┘    └────────────────┘    └────────────────┘       │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                                         │                                    │
│  ───────────────────────────────────────┴──────────────────────────────────  │
│                                         │                                    │
│  ┌──────────────────────────────────────┴───────────────────────────────┐   │
│  │                         EXTERNAL SERVICES                             │   │
│  │  ┌────────────┐    ┌────────────┐    ┌────────────┐                  │   │
│  │  │ Marketplace│    │  Telegram  │    │   Email    │                  │   │
│  │  │  (Browser) │    │   Alerts   │    │   Alerts   │                  │   │
│  │  └────────────┘    └────────────┘    └────────────┘                  │   │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                                                                              │
└──────────────────────────────────────────────────────────────────────────────┘
```

## Component Overview

### 1. Authentication & Session Management (`src/auth/`)

**Purpose:** Handle marketplace login and session persistence.

**Components:**
- `SessionManager`: Manages browser sessions, handles login, cookie persistence
- `CredentialsLoader`: Secure credential loading from env vars or encrypted files
- `ChallengeHandler`: Detects and handles Cloudflare, CAPTCHAs, rate limits

**Flow:**
```
Initialize → Load Cookies → Validate Session → (Re-login if needed) → Ready
```

### 2. Listing Scraper (`src/scraper/`)

**Purpose:** Extract listing data from marketplace pages.

**Components:**
- `ListingScraper`: Playwright-based scraper with pagination handling
- `HTMLParser`: BeautifulSoup parser for HTML extraction
- `ListingNormalizer`: Converts raw data to consistent schema

**Flow:**
```
Navigate → Wait for Load → Handle Pagination → Parse HTML → Normalize → Save
```

### 3. Quality Filter (`src/scoring/quality_filter.py`)

**Purpose:** Filter out unsuitable listings.

**Filters Applied:**
- Category blacklist/whitelist
- Seller rating threshold
- Price range
- Item condition
- Time remaining
- Competition level
- Suspicious patterns

**Output:** List of listings that passed all filters

### 4. Scoring Engine (`src/scoring/scoring_engine.py`)

**Purpose:** Calculate eligibility scores (0-100) for filtered items.

**Scoring Components:**
| Component | Weight | Description |
|-----------|--------|-------------|
| Seller Trust | 20% | Based on rating and feedback count |
| Price Value | 25% | Discount vs estimated value |
| Condition | 15% | Item condition quality |
| Competition | 15% | Current bid count |
| Time Urgency | 10% | Time remaining optimization |
| Category | 15% | Category preference priority |

**Output:** Scored and ranked listings with eligibility status

### 5. Bid Calculator (`src/bidding/bid_calculator.py`)

**Purpose:** Determine safe bid amounts.

**Calculations:**
1. Value-based maximum (% of estimated value)
2. Category limits
3. Risk multiplier adjustment
4. Budget constraint check
5. Exposure limit verification

**Output:** `BidDecision` with max bid, recommended bid, or rejection reason

### 6. DRY_RUN Engine (`src/bidding/dry_run_engine.py`)

**Purpose:** Simulate bidding without real transactions.

**Features:**
- Full pipeline execution
- Detailed simulation reports
- Budget impact projection
- Comparison with LIVE decisions

### 7. LIVE Engine (`src/bidding/live_engine.py`)

**Purpose:** Execute real bids on the marketplace.

**Safety Features:**
- Pre-bid validation
- Price verification
- Confirmation handling
- Pause/stop controls
- Human-like delays

### 8. Safety Controls (`src/safety/`)

**Purpose:** Prevent unexpected losses.

**Components:**
- `ExposureTracker`: Monitor pending liability
- `AuditLogger`: Log all actions with timestamps
- `AlertManager`: Send notifications for important events

**Hard Stops:**
- Daily/weekly budget exceeded
- Exposure limit reached
- Consecutive losses threshold
- Manual stop request

### 9. Configuration (`src/config/`)

**Purpose:** Manage agent settings.

**Features:**
- YAML/JSON configuration files
- Environment variable overrides
- Runtime validation
- Interactive CLI editor

## Data Flow

### DRY_RUN Mode

```
┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│   Scrape    │───▶│   Filter    │───▶│   Score     │───▶│  Simulate   │
│  Listings   │    │  Quality    │    │   Items     │    │    Bids     │
└─────────────┘    └─────────────┘    └─────────────┘    └─────────────┘
      │                  │                  │                   │
      ▼                  ▼                  ▼                   ▼
  Raw HTML          Filter Report      Score Report      Simulation Report
```

### LIVE Mode

```
┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│   Scrape    │───▶│   Filter    │───▶│   Score     │───▶│  Calculate  │
│  Listings   │    │  Quality    │    │   Items     │    │    Bids     │
└─────────────┘    └─────────────┘    └─────────────┘    └─────────────┘
                                                               │
      ┌────────────────────────────────────────────────────────┘
      ▼
┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│   Check     │───▶│   Execute   │───▶│   Track &   │
│  Exposure   │    │    Bid      │    │    Log      │
└─────────────┘    └─────────────┘    └─────────────┘
                         │
                         ▼
                   Marketplace
```

## Error Handling

### Retry Strategy

| Error Type | Action | Max Retries | Backoff |
|------------|--------|-------------|---------|
| Network | Retry | 3 | Exponential |
| Rate Limit | Wait | N/A | From header |
| Auth Failure | Re-login | 3 | Linear |
| Bid Rejected | Log & Skip | 0 | N/A |

### Circuit Breaker

The system implements automatic pausing when:
- Error rate exceeds 50% in last 10 operations
- Consecutive failures reach threshold
- Exposure limits approached

## Security Considerations

1. **Credentials**: Stored encrypted or in environment variables
2. **Sessions**: Cookies saved with restricted permissions
3. **Logging**: Sensitive data redacted in logs
4. **Network**: Uses HTTPS only
5. **Rate Limiting**: Built-in delays to avoid detection

## Performance

### Typical Timings

| Operation | Duration |
|-----------|----------|
| Login | 5-10 seconds |
| Page scrape | 3-5 seconds |
| Item scoring | < 10ms |
| Bid execution | 5-15 seconds |

### Resource Usage

- Memory: ~200-400MB (browser + Python)
- CPU: Low (mostly I/O bound)
- Network: Varies with marketplace

## Extensibility

### Adding a New Marketplace

1. Create custom selectors in config
2. Optionally subclass `HTMLParser` for complex parsing
3. Adjust timing parameters if needed

### Adding Custom Filters

```python
def my_custom_filter(listing: NormalizedListing) -> Optional[str]:
    if listing.some_condition:
        return "Custom filter reason"
    return None

quality_filter = QualityFilter(config, custom_filters=[my_custom_filter])
```

### Adding New Alert Channels

Extend `AlertManager` with new send methods following the existing pattern.
