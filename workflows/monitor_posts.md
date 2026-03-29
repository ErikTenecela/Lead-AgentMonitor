# Workflow: Monitor Posts for Home Service Leads

## Objective
Watch Facebook Groups, Nextdoor, and Craigslist every 5 minutes for posts requesting outdoor/home services in Fairfield County CT. Send SMS alerts to both phone numbers when a qualifying lead is found.

## Inputs Required
- `.env` file with all credentials filled in (see setup below)
- Facebook session saved (run `python tools/scrape_facebook.py --login`)
- Nextdoor session saved (run `python tools/scrape_nextdoor.py --login`)

## Configuration

### Score Threshold
Set `SCORE_THRESHOLD=6` in `.env`. Posts scoring 6-10 trigger an SMS. Raise to 7-8 to reduce noise.

### Daily SMS Cap
Set `DAILY_SMS_LIMIT=50` in `.env`. Alerts pause when this is hit. Resets at midnight.

### Post Age
Set `MAX_POST_AGE_MINUTES=120` to only process posts under 2 hours old.

### Facebook Groups
Configured in `workflows/facebook_groups.json`. Edit this file to add/remove groups.
Format: `[{"url": "...", "name": "...", "town": "..."}]`

## Running the System

### One-time cycle:
```
cd tools
python orchestrator.py
```

### Continuous loop (every 5 minutes):
```
cd tools
python orchestrator.py --loop
```

### Recommended: Windows Task Scheduler
- Action: `python C:\Users\Erik\Agents-Monitored\tools\orchestrator.py`
- Trigger: Every 5 minutes, all day
- This way it runs in the background and restarts automatically

## Services Monitored
- Masonry: chimney repair, brickwork, pointing, tuckpointing, retaining wall, stone work
- Hardscape: patio, walkway, concrete, driveway
- Outdoor: junk removal, yard cleanup, debris removal
- Tree: tree removal, tree trimming, stump removal
- Painting: interior/exterior, power washing
- Landscaping: lawn care, mulching, planting, grading, drainage

## Geographic Coverage
**Fairfield County towns:** Westport, Greenwich, Stamford, Norwalk, Darien, New Canaan, Fairfield, Bridgeport, Trumbull, Shelton, Stratford

**Craigslist:** newhaven.craigslist.org (covers full county)

## Business Hours Gate
SMS alerts only sent 7am–8pm CT. Overnight leads are batched and sent at 7am the next day.

## SMS Format
```
[LEAD 9/10] Chimney repair | Westport, CT
"Crumbling mortar, bricks loose, needs estimate ASAP"
Replies: 1 | Posted: 8min ago
Platform: Nextdoor
Link: https://nextdoor.com/p/abc123
```

## Known Constraints
- Facebook and Nextdoor sessions expire periodically — re-run `--login` if scraping stops
- Facebook changes its HTML structure often — if posts stop being found, inspect the page and update `scrape_facebook.py`
- Craigslist is most reliable (no login, RSS feed)

## Phase 2 Platforms (not yet built)
- Patch.com CT town forums
- Reddit (r/Connecticut, r/homeimprovement)
- Ring/Neighbors app
- Bark.com
- Houzz
