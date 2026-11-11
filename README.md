# MediBora: Providus shows a blood service which blood groups will run short, where, and in how many days, and how to share the blood it already has

Terumo BCT Africa Hackathon 2026 · Challenge area: forecasting and stock-out early warning

> All data is the organisers' synthetic starter dataset. Nothing here describes a real hospital, donor or patient.

## The problem
A hospital blood bank officer finds out a blood group has run out when a request cannot be filled. The fix is
an emergency call to the regional centre. In the starter dataset (numbers computed by our pipeline, in
`docs/DATA_FACTS.md`):

- 12.9% of requests were not fully met, and 43.8% of O negative units requested were never issued.
- An emergency resupply took a median of 5.2 hours.
- 23.2% of red cell and whole blood units were wasted in the same year.
- Demand is steady (549 to 749 units a week). Supply swings (345 to 2,230 collections a week) and falls 37.4%
  in school breaks. Shortages follow supply.
- **On 61% of the days a hospital ran short of a blood group, other hospitals in its region held spare units
  of that group.** The blood was enough. It was on the wrong shelf.

Our users are the **hospital blood bank officer**, who needs to know each morning which groups will run short
before the next delivery, and the **regional centre officer**, who supplies several hospitals and decides
where each week's units go.

## What we built
- What works now:
  - **Morning brief.** One plain sentence, then eight blood groups with days of cover, a cover bar that shows
    the next delivery, and a status: Act, Watch or Steady.
  - **14-day projection per blood group**, with expiry, expected deliveries and a busy-to-quiet range. Red
    cells and whole blood are counted together, because whole blood fills red cell requests.
  - **Two risks side by side:** the chance of running out, and the units likely to expire unused.
  - **Plain reasons and a suggested order shown as a sum.** The officer accepts, changes or rejects it.
    Changing or rejecting needs a reason. Every decision goes to an audit log that cannot be edited.
  - **Supply forecast** four weeks ahead from the drive schedule, and a **demand forecast** per hospital,
    product and blood group, each tested against a simple baseline.
  - **Fair-share allocation.** The regional view proposes how to share each week's units by days of cover,
    with a protected minimum for referral hospitals, and a replay shows what it would have changed.
  - **Regional view:** every hospital and blood group in one grid, the supply outlook, the gap by blood group,
    suggestions for extra collections with a draft donor call-out, and a platelet apheresis booking hint.
  - **What-if** on any blood group (a mass-casualty surge, a late delivery) and on the region (add or cancel
    a drive). Nothing is saved.
  - **Accuracy page** with every result beside its baseline, an alert scorecard, and a switch that falls back
    to the 4-week average. If the model fails, the app falls back by itself and says so on every screen.
  - **Works offline.** The tool runs on one computer with a local database, and the browser keeps the last brief.
  - **Data & privacy page**, role-based access, and no donor or patient identities anywhere in the system.
  - **Standards:** an accepted order as a FHIR R4-shaped `SupplyRequest`, and a DHIS2-shaped monthly export.
  - **Central copy** in Oracle Autonomous Database (see Architecture).
  - **Landing page and guided tour:** 13 short steps through every main feature.
- What is mocked or not finished:
  - **SMS:** the alert text and the donor call-out are previews, labelled "Demo, not sent". Sending through a
    gateway, with a person approving each message, is planned in `docs/SMS_HANDOVER.md`.
  - **FHIR and DHIS2:** field names follow the HL7 R4 specification, and the output has not been run through
    a FHIR validator. Product codes are local stand-ins for ISBT 128 codes, and DHIS2 identifiers are
    illustrative.
  - **Calendar:** school terms, public holidays and malaria months are settings read from the data and the
    Kenyan calendar, to be confirmed for each deployment.

Demo video (3 minutes or less): to add
Live demo: https://sanguis-providus.vercel.app (how it is hosted: `docs/DEPLOY.md`)

Test log-ins (demo accounts): press **Sign in** and choose a role, or sign in by name and password:
`amina` / `thika-demo`, `joseph` / `maternity-demo`, `grace` / `nairobi-demo`.

## How to run it
You need Python 3.11 or newer and Node.js 20 or newer.

**1. Set up Python** (run from the repo folder)

Windows (Git Bash):
```bash
python -m venv .venv
source .venv/Scripts/activate
pip install -r requirements.txt
```

Windows (PowerShell):
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Linux or macOS:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**2. Build the database from the raw files** (1 to 3 minutes the first time: it trains and tests the
forecasts. Run again with nothing changed and it finishes in under a second)
```bash
python -m backend.pipeline.build
```

**3. Start the app** (backend on port 8000, frontend on port 5173; the first run installs the frontend packages)
```bash
python scripts/dev.py
```
Open http://localhost:5173 and press **Take the guided tour**. Stop with Ctrl+C.

**Optional**
```bash
python -m pytest                                              # the tests
python -m backend.engine.brief --facility HSP-006 --as-of 2026-10-05   # a morning list in the terminal
python -m backend.sync.oracle                                 # the central copy (needs the Oracle settings in .env)
```
On a slow connection, install with `pip install --default-timeout=120 --retries 10 -r requirements.txt`.

## Architecture
![Architecture diagram](docs/architecture.svg)

- **Data flow:** raw files → pipeline (cleans, drops person-level columns) → local SQLite database → forecasts
  (demand and supply) → stock projection, alerts, suggested orders and the fair-share plan → API → web app.
  A person's decision goes back into the local database and its append-only audit log.
- **Local first:** the whole working path runs on one device with no internet.
- **Built to keep working:** if the forecast model errors, the 4-week average takes over and every screen says
  so. If the supply forecast errors, usual delivery quantities are used. If the server cannot be reached, the
  browser shows the last brief with a banner.
- **Where this sits in a blood-bank information system:** it reads counts (stock, requests, deliveries,
  drives) and hands back approved orders. It is a planning layer beside the system of record.
- **Standards:** FHIR R4 `SupplyRequest` for orders, DHIS2 `dataValueSet` for monthly indicators, and the
  dataset's unit label in the place of an ISBT 128 donation number. The mapping is in `docs/STANDARDS.md`.

### Oracle Autonomous Database: the central copy
Each site works on its own device. A national or regional view needs one shared place, so the tool sends a
one-way copy to **Oracle Autonomous Database**: weekly demand totals, daily shelf counts, the accuracy
scores, and the decisions and audit log with staff names replaced by codes.

Why Oracle Autonomous Database:
- **It looks after itself.** Patching, backups and tuning are automatic, which suits a blood service with no
  database administrator.
- **Encrypted by default,** at rest and in transit. The tool connects with a wallet (mutual TLS).
- **It scales from one county to a country** without changing the tool, and the Always Free tier covers a pilot.
- **Analytics on top.** Oracle Analytics Cloud and Oracle Machine Learning read the same tables, so a national
  team can build dashboards without touching any hospital's device.
- **Region.** The copy is in the Frankfurt region today. Oracle has announced a cloud region in Nairobi, so
  the copy can move in-country when it opens.

The first sync ran on 11 Nov 2026: seven tables, 192,816 rows. The app never depends on it.

Repo layout: `backend/pipeline` (data), `backend/engine` (forecasts, projection, alerts, allocation, regional
view), `backend/api` (server), `backend/sync` (central copy), `frontend` (web app), `config` (settings),
`tests`, `docs`.

## AI and data
All numbers are computed by `python -m backend.pipeline.build` and written to `docs/RESULTS.md`.
Test period: 16 Mondays from 6 July to 19 October 2026. On each date the forecasts see only earlier days.

- What the AI or data feature does: forecasts red cell supply per regional centre for 4 weeks; forecasts
  demand per hospital, product and blood group for 14 days; projects each shelf forward; warns before a
  shortage; and proposes how to share each week's units across hospitals.
- Baseline we compared against: the average of the last 4 weeks (supply and demand), and "cover under 3 days
  on the 28-day average" (alerts).
- Result:
  - **Supply** (cleared red cell units per centre and week): baseline 58% error vs ours 26%.
  - **Fair-share allocation** (replay, 6 July to 31 October 2026, same blood and same requests): units a
    hospital's own shelf could not supply fell from 2,078 to 1,410, which is 32% fewer. Units expired on
    hospital shelves fell from 1,415 to 1,256. The replay's own baseline (20.8%) matches the records (20.2%).
  - **Early warning:** our alert came before 76% of real shortages, against 65% for the simple rule, at the
    same precision (28% and 27% of warnings were followed by a shortage).
  - **Demand** (7-day totals): baseline 71.3% error vs ours 67.9%. Demand is steady, so both are close.
- How a user sees the reason for a recommendation: two or three short sentences beside each alert, built
  from the computed numbers, and the suggested quantity shown as a sum of its parts.
- Who approves it (Accept / Change / Reject): the signed-in officer. Every decision is logged with what the
  person was shown.
- What happens if the AI is wrong or offline: a switch on the Accuracy page, or any model error, makes every
  screen use the 4-week average and say so. The last brief is kept in the browser for offline use.

How the models work is described in `docs/MODELS.md`.

## Data
The organisers' synthetic starter dataset, in `data/raw/`. Tables used: facilities, routes, drives, donations,
units, transfers, requests, daily_stock, power_outages, temperature_logs.
The donor table is not in this repo and is never loaded. No real donor, patient or facility data.

## African conditions
- **Power and connectivity:** the tool runs on one laptop with a local database and no internet. The browser
  keeps the last brief and shows it with a banner when the server cannot be reached. The working screens are
  still and light, about 150 KB of compressed script, for slow links.
- **Phones:** every screen works at 360 pixels wide, with a bottom navigation bar and 44-pixel touch targets.
- **Languages:** every sentence a user reads is in one file (`frontend/src/i18n/en.ts`), so a language can be
  added without touching screens. The privacy notice is in English and Swahili.
- **Another site or country:** school terms, public holidays and malaria months are in
  `config/calendar_ke.yaml`; alert thresholds, minimum stock and the fair-share rules in
  `config/thresholds.yaml`; shelf lives and blood groups in `config/blood.yaml`; roles in
  `config/access.yaml`. Change settings, not code.
- **Existing systems:** FHIR and DHIS2 shaped outputs, and the Oracle central copy.
- **Rules that reflect how blood moves here:** an early delivery, a fair-share plan or an extra collection is
  a request for approval, never an order. Compatible stock is shown as information only.

## AI tools we used
| Tool | What we used it for |
| --- | --- |
| Claude Code (Anthropic) | Writing and testing the code in this repo during the hackathon, under the team's direction: the data pipeline, forecasts, API, web app, tests and documentation drafts. It also checked the screens with screenshots. |
| Design skill packs for Claude Code: impeccable (pbakaus), taste-skill (Leonxlnx), skills (emilkowalski) | Checklists for one design pass: contrast, touch behaviour, interaction states, motion. They are instruction documents; none of their files are in this repo. |
| Gemini and ChatGPT | Background research on existing blood-service tools and the supply-chain literature. Claims were checked against sources and against our own data before use. |

The team set the plan and the rules (`docs/BUILD_PLAN.md`, `CLAUDE.md`), chose the problem and the design
direction, checked the data findings, ran and reviewed each step, and can explain the code. No language model
runs inside the product: forecasts are statistical models and the reasons are written by templates.

## Team
| Name | Role | Country | Presenting Friday (in person / online) |
|------|------|---------|-----------------------------------------|
| Linet Mugwanja | Build lead and data lead | Kenya | to confirm |
| Jael Wainaina | Research and product | Kenya | to confirm |
| Eugene Mutembei | Integration (SMS alerts) | Kenya | to confirm |
| Washington Adiado | Team member | Kenya | to confirm |
| Trixy Wangui Wainaina | Team member | Kenya | to confirm |

## Known limits and next steps
- **Tested on synthetic data.** The next step is a pilot at one regional centre and its hospitals, on
  anonymised stock and request counts.
- **Alerts favour catching shortages.** 28% of warnings are followed by a shortage within 7 days, and the
  stated chance of running out ranks risk better than it measures it. Both will be recalibrated on real data.
- **Fair share is shown by replay.** It assumes units can be re-split within a delivery day and does not model
  vehicle capacity. Well-stocked hospitals give up some cover, which is why staff can set a protected minimum.
- **Where fair share sits in the literature.** Sharing stock before a shortage is called proactive lateral
  transshipment; days of cover is the Issuable Stock Index; our replay is a trace-driven simulation. South
  Australia's BloodMove programme, which rotates near-expiry red cells to higher-use laboratories, is reported
  to have cut avoidable wastage from 15% to under 1% (https://bloodmove.com/australian-bloodmove-project).
  Ours proposes the split before delivery.
- **Units exposed to a warm fridge** are not dated in the dataset, so they are not removed from projected stock.
- **Platelets** last 5 days and are supplied to order. They have a demand forecast and an apheresis booking
  hint; the units-per-donation figure is a setting.
- **Before real use:** a data protection impact assessment, registration with the Office of the Data
  Protection Commissioner, a processor agreement for the central copy, small-count suppression outside the
  hospital, a fluent review of the Swahili notice, HTTPS and an independent security test
  (`docs/SECURITY.md`).
- **Standards next:** ISBT 128 product codes, a FHIR validator run, and real DHIS2 element identifiers.
