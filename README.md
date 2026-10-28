# pgh-311-parking

Tired of filling in the 311 form and clicking through the same steps every time? This tool automates the process with a CLI and local one-click Web UI for reporting illegal parking to [Pittsburgh 311](https://pittsburghpa.my.site.com/311/s/). Designed to allow you to save preset addresses, contact information, and other default values for form fields, so that you can report recurring illegal parking at known locations with one click.

## Features

- **Fast Direct HTTP Mode (default)**: Calls PGH 311's Salesforce Aura / Visualforce backend endpoints directly—no Chrome, ArcGIS map iframe, or map tiles—for reports that usually complete in a few seconds.
- **One-Click Web UI**: Launch a local web dashboard (`./report_parking.py --serve`) to report recurring illegal parking spots with a single click, optionally attach a photo, and manage saved presets.
- **CLI Presets & Ad-Hoc Reporting**: Submit reports directly from the terminal using saved presets (`--preset`) or an address and short description.
- **Minimal Input Required**: Only an address and short description are needed. Other mandatory PGH 311 questions (`Illegal Parking` type, `Unsure` for license plate, and `N/A` for day/time/vehicle/duration) are automatically filled with sensible defaults and can be overridden via CLI flags or preset settings. Contact fields are left blank for anonymous submission.
- **Photo Attachments**: Supports uploading one or more photos (`-p / --photo` or via the Web UI) through PGH 311's configured upload endpoint.
- **Browser Fallback**: `--browser` preserves the original Playwright/Chrome workflow for troubleshooting or future PGH 311 frontend/API changes. `--headed` and `--screenshot` automatically imply browser mode.
- **Confirmation Extraction**: Returns the PGH 311 service request confirmation number (`26-0011xxxx`) and expected resolution date, with optional JSON output (`--json`).

## Requirements

- [uv](https://docs.astral.sh/uv/) (dependencies like `playwright` are managed automatically via inline PEP 723 script metadata)
- Google Chrome installed only if using browser fallback (`--browser`, `--headed`, or `--screenshot`)

## Quick Start

### 1. Local One-Click Web UI

Start the local web server:

```bash
./report_parking.py --serve
```

Then open **http://127.0.0.1:8311** in your browser:
- Click **Report Now** on any saved preset card to submit immediately and see the confirmation ID.
- Optionally select a photo at the top before clicking **Report Now**.
- Toggle **Dry-run mode** to resolve the address, validate GIS/service data, upload attachments, and build the request without submitting.
- Leave **Browser fallback** unchecked for the fast direct HTTP flow, or enable it to use the original Playwright/Chrome workflow.
- Add, edit, or delete recurring spots directly from the form (saved to `presets.json`).

### 2. Command Line Usage

**Submit using a saved preset:**

```bash
./report_parking.py --preset grant-sidewalk
./report_parking.py -P forbes-bikelane --photo /path/to/photo.jpg
```

**Submit an ad-hoc report with an address and description:**

```bash
./report_parking.py "414 Grant St" "Vehicle parked blocking the sidewalk"
```

**Submit with optional photo and vehicle/plate details:**

```bash
./report_parking.py "414 Grant St" "Car parked in bike lane" \
  --photo /path/to/photo.jpg \
  --plate "ABC-1234" \
  --vehicle "Silver sedan"
```

**Test without submitting (`--dry-run`) and output JSON (`--json`):**

```bash
./report_parking.py "414 Grant St" "Vehicle blocking the sidewalk" --dry-run --json
```

**Use the slower browser fallback (for troubleshooting):**

```bash
./report_parking.py "414 Grant St" "Vehicle blocking the sidewalk" --browser --dry-run
```

### Managing Presets & Default Contact Info from the CLI

Spot presets are stored in [`presets.json`](./presets.json), while your personal contact profile (Step 4 of 5) is automatically saved to **`presets.local.json`** (which is gitignored so your name, email, and phone number are never committed to Git).
*(Note: On PGH 311, if you provide an email or phone number, a Last Name is required by the form—if you don't specify `--last-name`, the script automatically fills `"Resident"`).*

```bash
# List saved presets and default contact profile
./report_parking.py --list-presets

# Save your default email / phone (used automatically for all reports)
./report_parking.py --save-contact --email "you@example.com" --phone "412-555-1234" --first-name "Jane" --last-name "Doe"

# Clear saved contact info (revert to anonymous reporting)
./report_parking.py --clear-contact

# Save a new spot preset
./report_parking.py "900 Penn Ave" "Vehicles parked in the protected bike lane" \
  --save-preset penn-bikelane \
  --preset-label "Penn Ave Bike Lane"

# Delete a spot preset
./report_parking.py --delete-preset penn-bikelane
```

## Direct HTTP Mode Notes

By default the script fetches the PGH 311 portal bootstrap, extracts the current Salesforce Aura and Visualforce remoting contexts, then calls the same backend operations used by the website:

1. `fetchAddress` resolves the typed address to Pittsburgh's canonical `address_string`, latitude/longitude, street ID, and ZIP.
2. `getgeolayers` validates the selected location against the city GIS layers.
3. `getFileUploadData` discovers the current upload endpoint and credential dynamically, then uploads optional attachments and passes their public URLs as `media_url`.
4. `createServiceRequest` submits the completed `ILGLPARK` state and returns the confirmation details.

This avoids loading the ArcGIS JavaScript API, map iframe, and map tiles. These backend calls are undocumented and can change if Pittsburgh updates the portal; use `--browser` or the Web UI's **Browser fallback** checkbox if direct mode stops working. `--dry-run` exercises address/GIS/upload/payload construction but never calls `createServiceRequest`.

## CLI Options

```text
positional arguments:
  address               Street address or intersection in Pittsburgh (or omit when using --preset / --serve).
  description           Short description of the illegal parking issue.

options:
  -h, --help            Show help message and exit.
  -P, --preset PRESET   Submit using a saved preset name from presets.json.
  --list-presets        List all saved presets and default contact info from presets.json and exit.
  --save-preset NAME    Save the provided address/description/options as a named preset in presets.json.
  --preset-label LABEL  Optional human-readable display label when using --save-preset.
  --delete-preset NAME  Delete a named preset from presets.json and exit.
  --save-contact        Save --email, --phone, --first-name, --last-name as the default contact profile in presets.local.json.
  --clear-contact       Clear the saved default contact profile in presets.local.json (revert to anonymous).
  --anonymous           Ignore any saved contact profile for this run and submit anonymously.
  --first-name NAME     Step 4 Contact: First name.
  --last-name NAME      Step 4 Contact: Last name (auto-fills 'Resident' if email/phone is set without last name).
  --email EMAIL         Step 4 Contact: Email address.
  --phone PHONE         Step 4 Contact: 10-digit US phone number (e.g. '412-555-1234').
  --presets-file PATH   Path to presets JSON file (default: ./presets.json).
  --serve               Start the local one-click Web UI server.
  --port PORT           Port for --serve Web UI (default: 8311).
  --host HOST           Host for --serve Web UI (default: 127.0.0.1).
  -p, --photo PHOTOS    Optional path to a photo/attachment to upload (can be specified multiple times).
  --report-type {Illegal Parking,Boat or Trailer Parked on Street}
                        Question 1: Type of violation (default: 'Illegal Parking').
  --day DAY             Question 3: Day of the week when issue occurs (default: 'N/A').
  --time TIME           Question 4: Time of day/night when issue occurs (default: 'N/A').
  --has-plate {Yes,No,Unsure}
                        Question 5: Does the vehicle have a license plate? (default: 'Unsure').
  --plate PLATE         License plate number (automatically sets --has-plate to 'Yes').
  --vehicle VEHICLE     Question 6: Vehicle/boat/trailer description (default: 'N/A').
  --duration DURATION   Question 7: How long the vehicle has been in place (default: 'N/A').
  --additional-info TEXT
                        Optional 'Additional Information' field on the Details step.
  --location-details TEXT
                        Optional 'Additional Information' field on the Location step.
  --dry-run             Build and validate the report without submitting it to PGH 311.
  --browser             Use the slower Playwright/Chrome workflow instead of direct HTTP.
  --headed              Run browser in visible (non-headless) mode (implies --browser).
  --screenshot PATH     Optional browser-mode screenshot path (implies --browser).
  --json                Output result as JSON.
  --quiet               Suppress progress logs on stderr.
```
