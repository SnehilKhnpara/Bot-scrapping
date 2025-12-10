# Troubleshooting Guide

This guide covers common issues and their solutions.

## Quick Diagnostics

Run these commands to diagnose issues:

```bash
# Check Python version
python --version  # Should be 3.10+

# Validate configuration
python main.py validate

# Check Playwright installation
playwright --version

# Test with visible browser
python main.py run --no-headless --mode DRY_RUN
```

## Common Issues

### Authentication Issues

#### "Login failed" or "Session expired"

**Symptoms:**
- Agent can't log in
- Repeated authentication attempts
- "Session expired" in logs

**Solutions:**

1. **Verify credentials:**
```bash
# Check .env file exists and has correct values
cat .env
```

2. **Clear saved cookies:**
```bash
rm -rf data/sessions/
```

3. **Check selectors match your marketplace:**
```yaml
# In config.yaml, verify these match actual HTML
login_selectors:
  username_input: 'input[name="email"]'  # Inspect actual element
  password_input: 'input[type="password"]'
```

4. **Run with visible browser to debug:**
```bash
python main.py run --no-headless
```

5. **Check for 2FA requirements** - if your account has 2FA, you may need to configure TOTP secret.

#### "Cloudflare challenge detected"

**Solutions:**
- Wait - the agent attempts to wait for Cloudflare checks
- Try running at different times
- Use residential IP if possible
- Increase `page_load_timeout` in config

#### "CAPTCHA detected"

**Current Behavior:** Agent pauses when CAPTCHA detected.

**Options:**
1. Solve CAPTCHA manually (if running with `--no-headless`)
2. Use a different IP address
3. Reduce request frequency

### Scraping Issues

#### "No listings found"

**Causes:**
- Wrong URL in configuration
- Selectors don't match marketplace HTML
- Page structure changed
- Rate limited

**Solutions:**

1. **Verify URLs:**
```bash
# Test URL manually in browser
curl -I "https://your-marketplace.com/listings"
```

2. **Update selectors:**
- Open marketplace in browser
- Right-click on listing → Inspect
- Copy correct CSS selector
- Update `listing_selectors` in config

3. **Check saved HTML:**
```bash
# Look at what was actually scraped
ls data/raw/
cat data/raw/latest.html | head -100
```

4. **Check for JavaScript-rendered content:**
- Increase `page_load_timeout`
- Add wait selectors in scraper config

#### "Missing price/seller data"

**Cause:** Selectors not matching expected elements

**Solution:** Update parser selectors for your marketplace:
```yaml
listing_selectors:
  current_price: '.actual-price-class'
  seller_name: '.seller-info .name'
```

### Bidding Issues

#### "All items filtered out"

**Check filter statistics:**
```bash
# Look at DRY_RUN reports
cat data/reports/dry_run_*_report.json | python -m json.tool
```

**Common causes:**
- `min_seller_rating` too high
- `max_price` too low
- All categories blacklisted
- `max_bid_count` too low

**Solution:** Adjust filter settings in config:
```yaml
filter:
  min_seller_rating: 90.0  # Lower from 95
  max_price: 1000.0        # Increase
  max_bid_count: 50        # Increase
```

#### "Score below threshold"

**Check scores:**
```bash
# In DRY_RUN report, look at low_score_items
```

**Solutions:**
- Lower `min_eligible_score`:
```yaml
scoring:
  min_eligible_score: 50.0  # Lower from 60
```
- Adjust component weights
- Add category priorities for your target categories

#### "Budget exhausted"

**Check budget status:**
```bash
# Look in logs for budget messages
grep -i "budget" logs/audit_*.jsonl
```

**Solutions:**
- Wait for daily reset
- Increase limits:
```yaml
bidding:
  daily_budget: 200.0
  max_daily_exposure: 400.0
```

#### "Bid rejected by marketplace"

**Causes:**
- Auction ended
- Outbid during submission
- Invalid bid amount
- Account restrictions

**Check:**
```bash
# Look at bid execution logs
grep "bid_execution" logs/audit_*.jsonl
```

### Performance Issues

#### "Agent is slow"

**Solutions:**

1. **Run in headless mode:**
```bash
python main.py run --headless
```

2. **Reduce max pages:**
```yaml
marketplace:
  max_pages: 5
```

3. **Reduce refresh interval:**
```yaml
refresh_interval: 600  # 10 minutes instead of 5
```

#### "High memory usage"

**Solutions:**
- Reduce `max_pages`
- Disable `save_raw_html`:
```yaml
storage:
  save_raw_html: false
```
- Restart agent periodically

#### "Browser crashes"

**Solutions:**

1. **Increase system resources**

2. **Add swap space (Linux):**
```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
```

3. **Reduce concurrent operations**

### Configuration Issues

#### "Configuration validation failed"

**Run validation:**
```bash
python main.py validate
```

**Common issues:**
- Missing required fields (`base_url`, `listings_url`)
- Invalid YAML syntax (check indentation)
- Values out of range

**YAML Syntax Check:**
```bash
python -c "import yaml; yaml.safe_load(open('config/config.yaml'))"
```

#### "Environment variable not found"

**Check:**
```bash
# Verify .env file
cat .env

# Or export manually
export MARKETPLACE_USERNAME=your_user
export MARKETPLACE_PASSWORD=your_pass
```

### Logging Issues

#### "Logs not appearing"

**Check:**
- Logs directory exists: `ls logs/`
- Disk space available: `df -h`
- Permissions: `ls -la logs/`

**Force verbose output:**
```bash
python main.py run --mode DRY_RUN 2>&1 | tee debug.log
```

#### "Log files too large"

**Solutions:**
- Reduce log retention:
```yaml
storage:
  log_retention_days: 7
```
- Compress old logs (automatic)
- Clean up manually:
```bash
find logs/ -name "*.gz" -mtime +7 -delete
```

## Debug Mode

For detailed debugging, modify logging level:

```python
# In main.py, add before structlog.configure():
import logging
logging.basicConfig(level=logging.DEBUG)
```

## Getting Help

If issues persist:

1. **Check logs:**
```bash
tail -100 logs/audit_*.jsonl
```

2. **Run with visible browser:**
```bash
python main.py run --no-headless --mode DRY_RUN
```

3. **Take screenshots:**
```yaml
storage:
  save_screenshots: true
```

4. **Create minimal reproduction:**
- Note exact error message
- Include relevant config (redact credentials)
- Describe steps to reproduce

## Recovery Procedures

### Reset Agent State

```bash
# Clear all cached data
rm -rf data/sessions/
rm -rf data/raw/
rm -rf data/processed/

# Keep logs for investigation
# rm -rf logs/  # Only if needed
```

### Hard Stop Recovery

If hard stop was triggered:

1. Check why in logs
2. Fix underlying issue
3. Reset can be done by restarting agent

### Exposure Limit Recovery

Exposure limits reset automatically:
- Daily: At midnight
- Weekly: On Monday
- Session: On agent restart

To manually reset, restart the agent.
