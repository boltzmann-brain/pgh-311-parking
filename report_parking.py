#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "playwright>=1.40.0",
# ]
# ///
"""
Automated CLI and Local Web UI to report illegal parking to Pittsburgh 311:
https://pittsburghpa.my.site.com/311/s/
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import tempfile
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

SITE_URL = "https://pittsburghpa.my.site.com/311/s/"
DEFAULT_CHROME_MAC = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
DEFAULT_PRESETS_PATH = Path(__file__).resolve().parent / "presets.json"
DEFAULT_LOCAL_PRESETS_PATH = Path(__file__).resolve().parent / "presets.local.json"

_SUBMIT_LOCK = threading.Lock()


def _read_raw_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"contact": {}, "presets": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"contact": {}, "presets": {}}
        return {
            "contact": data.get("contact") if isinstance(data.get("contact"), dict) else {},
            "presets": data.get("presets") if isinstance(data.get("presets"), dict) else {},
        }
    except Exception as exc:
        print(f"Warning: could not read config from {path}: {exc}", file=sys.stderr)
        return {"contact": {}, "presets": {}}


def get_local_presets_path(path: Path = DEFAULT_PRESETS_PATH) -> Path:
    if path.resolve() == DEFAULT_PRESETS_PATH.resolve():
        return DEFAULT_LOCAL_PRESETS_PATH
    return path.with_name(f"{path.stem}.local{path.suffix}")


def load_config(path: Path = DEFAULT_PRESETS_PATH) -> dict[str, Any]:
    base = _read_raw_config(path)
    local_path = get_local_presets_path(path)
    local = _read_raw_config(local_path) if local_path != path else {"contact": {}, "presets": {}}

    merged_contact: dict[str, Any] = {}
    for k in ("first_name", "last_name", "email", "phone"):
        val = local["contact"].get(k)
        if not val:
            val = base["contact"].get(k)
        merged_contact[k] = val or None

    merged_presets = dict(base["presets"])
    merged_presets.update(local["presets"])
    return {
        "contact": merged_contact,
        "presets": merged_presets,
    }


def save_contact_config(contact: dict[str, Any], path: Path = DEFAULT_PRESETS_PATH) -> Path:
    """Save personal contact details to the gitignored presets.local.json file."""
    local_path = get_local_presets_path(path)
    local = _read_raw_config(local_path)
    local["contact"] = {
        "first_name": contact.get("first_name") or None,
        "last_name": contact.get("last_name") or None,
        "email": contact.get("email") or None,
        "phone": contact.get("phone") or None,
    }
    local_path.write_text(json.dumps(local, indent=2) + "\n", encoding="utf-8")
    return local_path


def save_presets_config(presets: dict[str, Any], path: Path = DEFAULT_PRESETS_PATH) -> None:
    """Save spot presets to presets.json while keeping presets.json contact fields redacted."""
    base = _read_raw_config(path)
    out = {
        "contact": {
            "first_name": base["contact"].get("first_name") or None,
            "last_name": base["contact"].get("last_name") or None,
            "email": base["contact"].get("email") or None,
            "phone": base["contact"].get("phone") or None,
        },
        "presets": presets or {},
    }
    path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")


def find_browser_executable(custom_path: str | None = None) -> str | None:
    if custom_path:
        return custom_path
    if os.path.exists(DEFAULT_CHROME_MAC):
        return DEFAULT_CHROME_MAC
    return None


def wait_for_spinners_hidden(page: Page, timeout: int = 25000) -> None:
    """Wait until any active loading spinner overlays inside the modal disappear."""
    spinners = page.locator("c-web-spinner div.centred-overlay")
    count = spinners.count()
    for i in range(count):
        try:
            spinners.nth(i).wait_for(state="hidden", timeout=timeout)
        except PlaywrightTimeoutError:
            pass


def choose_best_address_suggestion(suggestions: list[str], query: str) -> int:
    """
    Pick the best matching address from the GIS suggestion list.
    Prefers standard street addresses (e.g. '414 GRANT ST, 15219') over parcel-prefixed
    entries (e.g. '2-J-2, 414 GRANT ST, PITTSBURGH, PA, 15219') when available.
    """
    parcel_pattern = re.compile(r"^\d+-[A-Z0-9]+-\d+\s*,", re.IGNORECASE)
    non_parcel_indices = [
        i for i, text in enumerate(suggestions) if not parcel_pattern.match(text.strip())
    ]
    if non_parcel_indices:
        return non_parcel_indices[0]
    return 0


def fill_text_question(page: Page, data_code: str, value: str) -> None:
    """Fill a single-line text question (c-base-input-new) and trigger blur to register the answer."""
    inp = page.locator(f'div[data-code="{data_code}"] input').first
    inp.wait_for(state="visible", timeout=10000)
    inp.fill(value)
    inp.blur()


def fill_textarea_question(page: Page, data_code: str, value: str) -> None:
    """Fill a multi-line textarea question (c-web-que-text-area)."""
    ta = page.locator(f'div[data-code="{data_code}"] textarea').first
    ta.wait_for(state="visible", timeout=10000)
    ta.fill(value)
    ta.blur()


def select_radio_question(page: Page, data_code: str, option_value: str) -> None:
    """Select an inline radio button option by its data-id attribute."""
    radio = page.locator(
        f'div[data-code="{data_code}"] input[type="radio"][data-id="{option_value}"]'
    ).first
    radio.wait_for(state="attached", timeout=10000)
    radio.click()


def normalize_phone_digits(phone: str) -> str:
    """Extract 10-digit US phone number for the PGH 311 phone input."""
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        raise ValueError(
            f"Phone number must contain 10 digits (got '{phone}'). Example: 412-555-1234"
        )
    return digits


def submit_parking_report(
    address: str,
    description: str,
    photo_paths: list[str] | None = None,
    report_type: str = "Illegal Parking",
    day_of_week: str = "N/A",
    time_of_day: str = "N/A",
    has_plate: str = "Unsure",
    plate_number: str | None = None,
    vehicle_description: str = "N/A",
    duration: str = "N/A",
    additional_info: str | None = None,
    location_details: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    headless: bool = True,
    dry_run: bool = False,
    screenshot_path: str | None = None,
    browser_executable: str | None = None,
    verbose: bool = True,
) -> dict[str, Any]:
    def log(msg: str) -> None:
        if verbose:
            print(msg, file=sys.stderr)

    validated_photos: list[str] = []
    if photo_paths:
        for p in photo_paths:
            path_obj = Path(p).expanduser().resolve()
            if not path_obj.is_file():
                raise FileNotFoundError(f"Photo file not found: {p}")
            validated_photos.append(str(path_obj))

    if plate_number and has_plate == "Unsure":
        has_plate = "Yes"

    # PGH 311's contact step requires Last Name whenever any contact field (email/phone/first_name) is provided
    has_any_contact = any([first_name, last_name, email, phone])
    effective_last_name = last_name or ("Resident" if has_any_contact else None)
    clean_phone = normalize_phone_digits(phone) if phone else None

    exec_path = find_browser_executable(browser_executable)

    with _SUBMIT_LOCK:
        with sync_playwright() as pw:
            launch_kwargs: dict[str, Any] = {"headless": headless}
            if exec_path:
                launch_kwargs["executable_path"] = exec_path

            browser = pw.chromium.launch(**launch_kwargs)
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            page = context.new_page()

            try:
                log(f"[1/6] Loading PGH 311 portal ({SITE_URL})...")
                page.goto(SITE_URL, wait_until="networkidle", timeout=45000)

                illegal_parking_btn = page.get_by_text("Illegal Parking", exact=True).first
                illegal_parking_btn.wait_for(state="visible", timeout=20000)
                illegal_parking_btn.click()

                req_service_btn = page.get_by_text("Request This Service", exact=True).first
                req_service_btn.wait_for(state="visible", timeout=15000)
                req_service_btn.click()

                # Step 1 of 5: File Upload
                log("[2/6] Step 1 of 5: Attachments...")
                continue_btn = page.locator("button.Continue").first
                continue_btn.wait_for(state="visible", timeout=15000)
                wait_for_spinners_hidden(page)

                if validated_photos:
                    log(f"      Uploading {len(validated_photos)} file(s)...")
                    page.locator('input[type="file"][name="filename"][accept*="."]').first.wait_for(
                        state="attached", timeout=15000
                    )
                    with page.expect_file_chooser(timeout=10000) as fc_info:
                        page.locator(".inner-dotrd-area").first.click()
                    fc_info.value.set_files(validated_photos)

                    page.locator("c-web-file-upload-preview-new .attached").first.wait_for(
                        state="visible", timeout=30000
                    )
                    wait_for_spinners_hidden(page)
                    log("      Upload complete.")

                continue_btn.click()

                # Handle optional EXIF location suggestion popup if photo had GPS metadata
                try:
                    loc_popup_cancel = page.locator(
                        "c-web-location-suggestion-popup img.file-error-popup-btn"
                    ).first
                    if loc_popup_cancel.is_visible(timeout=1500):
                        log("      Dismissing photo EXIF location suggestion popup...")
                        loc_popup_cancel.click()
                except PlaywrightTimeoutError:
                    pass

                # Step 2 of 5: Location
                log(f"[3/6] Step 2 of 5: Selecting location for '{address}'...")
                search_input = page.get_by_placeholder("Search", exact=True).first
                search_input.wait_for(state="visible", timeout=20000)

                # Wait for the Esri map iframe to finish initializing (which hides c-esri-map's spinner)
                map_spinner = page.locator("c-esri-map c-web-spinner div.centred-overlay").first
                try:
                    map_spinner.wait_for(state="visible", timeout=3000)
                except PlaywrightTimeoutError:
                    pass
                map_spinner.wait_for(state="hidden", timeout=25000)
                wait_for_spinners_hidden(page, timeout=15000)

                search_input.fill(address)

                suggestion_items = page.locator("li.loc-set")
                try:
                    suggestion_items.first.wait_for(state="visible", timeout=15000)
                except PlaywrightTimeoutError:
                    raise RuntimeError(
                        f"No matching Pittsburgh address suggestions found for '{address}'."
                    )

                suggestions_text = [
                    t.strip() for t in suggestion_items.all_inner_texts() if t.strip()
                ]
                if not suggestions_text:
                    raise RuntimeError(
                        f"No matching Pittsburgh address suggestions found for '{address}'."
                    )

                chosen_idx = choose_best_address_suggestion(suggestions_text, address)
                chosen_address = suggestions_text[chosen_idx]
                log(f"      Matched address: {chosen_address}")
                suggestion_items.nth(chosen_idx).click()

                enabled_continue = page.locator(
                    "button.Continue:not([disabled]):not(.disabled-btn)"
                ).first
                enabled_continue.wait_for(state="visible", timeout=15000)

                if location_details:
                    add_loc_inp = page.locator("input.ladd-info").first
                    if add_loc_inp.is_visible():
                        add_loc_inp.fill(location_details)
                        add_loc_inp.blur()

                enabled_continue.click()

                # Step 3 of 5: Details
                log("[4/6] Step 3 of 5: Filling issue details...")
                page.locator('div[data-code="01PL-ILGLPRKTYP"]').first.wait_for(
                    state="visible", timeout=15000
                )
                wait_for_spinners_hidden(page)

                select_radio_question(page, "01PL-ILGLPRKTYP", report_type)
                fill_textarea_question(page, "01TA-ILGLPRKDES", description)
                fill_text_question(page, "01TX-NOISEDAY", day_of_week)
                fill_text_question(page, "01TX-NOISETIME", time_of_day)
                select_radio_question(page, "01PL-ILGLPRKPLT", has_plate)

                if has_plate == "Yes":
                    fill_text_question(page, "01TX-ILGLPRKNUM", plate_number or "N/A")

                fill_textarea_question(page, "01TA-ILGLPRKVEH", vehicle_description)
                fill_text_question(page, "01TX-ILGLPRKDUR", duration)

                if additional_info:
                    add_info_ta = page.locator(
                        'div[data-name="add-info"] textarea[placeholder="Additional Information"]'
                    ).first
                    add_info_ta.fill(additional_info)
                    add_info_ta.blur()

                enabled_continue.wait_for(state="visible", timeout=10000)
                enabled_continue.click()

                # Step 4 of 5: Contact
                page.get_by_text("How can we reach you?", exact=True).first.wait_for(
                    state="visible", timeout=15000
                )
                wait_for_spinners_hidden(page)

                if has_any_contact:
                    log("[5/6] Step 4 of 5: Filling contact information...")
                    if first_name:
                        fn_inp = page.locator("c-web-primary-contact-new .firstNameClass input").first
                        fn_inp.fill(first_name)
                        fn_inp.blur()
                    if effective_last_name:
                        ln_inp = page.locator("c-web-primary-contact-new .lastNameClass input").first
                        ln_inp.fill(effective_last_name)
                        ln_inp.blur()
                    if email:
                        em_inp = page.locator("c-web-primary-contact-new .emailClass input").first
                        em_inp.fill(email)
                        em_inp.blur()
                    if clean_phone:
                        ph_inp = page.locator("c-web-primary-contact-new .phoneClass input").first
                        ph_inp.fill(clean_phone)
                        ph_inp.blur()
                else:
                    log("[5/6] Step 4 of 5: Leaving contact information blank...")

                enabled_continue.wait_for(state="visible", timeout=10000)
                enabled_continue.click()

                # Step 5 of 5: Review
                log("[6/6] Step 5 of 5: Review...")
                submit_btn = page.get_by_role("button", name="Submit Request").first
                submit_btn.wait_for(state="visible", timeout=15000)
                wait_for_spinners_hidden(page)

                result: dict[str, Any] = {
                    "status": "dry_run" if dry_run else "submitting",
                    "matched_address": chosen_address,
                    "description": description,
                    "report_type": report_type,
                    "photos_uploaded": [os.path.basename(p) for p in validated_photos],
                    "contact": {
                        "first_name": first_name,
                        "last_name": effective_last_name,
                        "email": email,
                        "phone": clean_phone,
                    }
                    if has_any_contact
                    else None,
                }

                if dry_run:
                    log("      Dry-run enabled: stopping at Review screen without submitting.")
                    if screenshot_path:
                        page.screenshot(path=screenshot_path, full_page=False)
                        result["screenshot"] = screenshot_path
                    return result

                log("      Submitting service request...")
                submit_btn.click()

                outcome_locator = page.locator(
                    "c-web-s-r-submit-summary-new, c-web-s-r-submit-error-state-new"
                ).first
                outcome_locator.wait_for(state="visible", timeout=30000)

                error_locator = page.locator("c-web-s-r-submit-error-state-new").first
                if error_locator.is_visible():
                    err_text = error_locator.inner_text().strip()
                    raise RuntimeError(f"Service request submission failed on server: {err_text}")

                sr_num_locator = page.locator(
                    "c-web-s-r-submit-summary-new .sr-number-section"
                ).first
                sr_num_locator.wait_for(state="visible", timeout=15000)
                sr_number = sr_num_locator.inner_text().strip()

                summary_comp = page.locator("c-web-s-r-submit-summary-new").first
                expected_date = None
                cal_locator = summary_comp.locator(".sr-calender .file-length").first
                if cal_locator.count() > 0 and cal_locator.is_visible():
                    expected_date = cal_locator.inner_text().strip()

                raw_details = summary_comp.evaluate("el => el.submittedCaseDetails || null")

                if screenshot_path:
                    page.screenshot(path=screenshot_path, full_page=False)

                result.update(
                    {
                        "status": "submitted",
                        "service_request_id": sr_number,
                        "expected_resolution_date": expected_date,
                        "confirmation_text": summary_comp.inner_text().strip(),
                    }
                )
                if raw_details and isinstance(raw_details, dict):
                    result["case_details"] = {
                        k: raw_details.get(k)
                        for k in (
                            "id",
                            "service_request_id",
                            "status",
                            "service_name",
                            "address_string",
                            "requested_datetime",
                            "expected_datetime",
                        )
                        if k in raw_details
                    }
                if screenshot_path:
                    result["screenshot"] = screenshot_path

                log(f"      Success! Confirmation ID: {sr_number}")
                return result

            finally:
                context.close()
                browser.close()


WEB_UI_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>PGH 311 Illegal Parking Reporter</title>
<style>
  :root {
    --bg: #0f172a;
    --panel: #1e293b;
    --panel-hover: #263449;
    --border: #334155;
    --text: #f8fafc;
    --muted: #94a3b8;
    --accent: #eab308;
    --accent-hover: #facc15;
    --accent-fg: #0f172a;
    --success: #22c55e;
    --danger: #ef4444;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    padding: 24px;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    max-width: 960px;
    margin-inline: auto;
  }
  header {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding-bottom: 20px;
    border-bottom: 1px solid var(--border);
    margin-bottom: 24px;
  }
  h1 {
    margin: 0;
    font-size: 1.5rem;
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .badge {
    background: var(--accent);
    color: var(--accent-fg);
    font-weight: 700;
    padding: 2px 8px;
    border-radius: 6px;
    font-size: 0.85rem;
  }
  .controls-bar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 16px;
    background: var(--panel);
    padding: 12px 16px;
    border-radius: 10px;
    border: 1px solid var(--border);
    margin-bottom: 24px;
  }
  .controls-bar label {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 0.9rem;
    cursor: pointer;
  }
  .file-picker {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-left: auto;
    font-size: 0.9rem;
  }
  .section-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 14px;
  }
  h2 {
    margin: 0;
    font-size: 1.15rem;
    color: var(--text);
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 16px;
    margin-bottom: 32px;
  }
  .card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 18px;
    display: flex;
    flex-direction: column;
    gap: 10px;
    transition: border-color 0.15s, background 0.15s;
  }
  .card:hover {
    border-color: #475569;
    background: var(--panel-hover);
  }
  .card-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 8px;
  }
  .card-title {
    font-weight: 600;
    font-size: 1.05rem;
    margin: 0;
  }
  .card-key {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.75rem;
    color: var(--muted);
    background: #0f172a;
    padding: 2px 6px;
    border-radius: 4px;
  }
  .card-address {
    font-size: 0.9rem;
    color: var(--accent);
    font-weight: 500;
  }
  .card-desc {
    font-size: 0.88rem;
    color: var(--muted);
    line-height: 1.4;
    flex-grow: 1;
  }
  .card-actions {
    display: flex;
    gap: 8px;
    margin-top: 8px;
  }
  button {
    cursor: pointer;
    border: none;
    border-radius: 8px;
    font-weight: 600;
    font-size: 0.9rem;
    padding: 10px 14px;
    transition: opacity 0.15s, background 0.15s;
  }
  button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .btn-primary {
    background: var(--accent);
    color: var(--accent-fg);
    flex: 1;
  }
  .btn-primary:hover:not(:disabled) {
    background: var(--accent-hover);
  }
  .btn-secondary {
    background: transparent;
    color: var(--muted);
    border: 1px solid var(--border);
    padding: 8px 12px;
  }
  .btn-secondary:hover:not(:disabled) {
    color: var(--text);
    border-color: var(--muted);
  }
  .btn-danger {
    background: transparent;
    color: #f87171;
    border: 1px solid #7f1d1d;
    padding: 8px 10px;
  }
  .btn-danger:hover:not(:disabled) {
    background: #7f1d1d33;
  }
  .status-box {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 16px 20px;
    margin-bottom: 24px;
    display: none;
  }
  .status-box.active { display: block; }
  .status-box.success { border-color: var(--success); }
  .status-box.error { border-color: var(--danger); }
  .status-title {
    font-weight: 600;
    margin-bottom: 6px;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .status-details {
    font-size: 0.9rem;
    color: var(--muted);
    white-space: pre-wrap;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  .panel-form {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 20px;
    margin-bottom: 28px;
  }
  .form-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 14px;
    margin-bottom: 14px;
  }
  .field {
    display: flex;
    flex-direction: column;
    gap: 5px;
  }
  .field label {
    font-size: 0.82rem;
    color: var(--muted);
    font-weight: 500;
  }
  .field input, .field textarea, .field select {
    background: #0f172a;
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text);
    padding: 9px 11px;
    font-size: 0.9rem;
    font-family: inherit;
  }
  .field textarea {
    min-height: 68px;
    resize: vertical;
  }
  .spinner {
    display: inline-block;
    width: 14px;
    height: 14px;
    border: 2px solid currentColor;
    border-right-color: transparent;
    border-radius: 50%;
    animation: spin 0.75s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>
  <header>
    <h1><span class="badge">PGH 311</span> Illegal Parking One-Click Reporter</h1>
  </header>

  <div class="controls-bar">
    <label title="Run through all 5 steps and stop on Review without submitting">
      <input type="checkbox" id="dryRunToggle" />
      Dry-run mode (test without submitting)
    </label>
    <div class="file-picker">
      <label for="photoInput">Optional Photo:</label>
      <input type="file" id="photoInput" accept="image/*" />
      <button type="button" class="btn-secondary" id="clearPhotoBtn" style="display:none;padding:4px 8px;font-size:0.8rem;">Clear</button>
    </div>
  </div>

  <div id="statusBox" class="status-box">
    <div id="statusTitle" class="status-title"></div>
    <div id="statusDetails" class="status-details"></div>
  </div>

  <div class="section-header">
    <h2>Saved Recurring Spots (One-Click Report)</h2>
  </div>
  <div id="presetsGrid" class="grid"></div>

  <div class="panel-form">
    <h2 style="margin-bottom:6px;">Default Contact Profile (Step 4 of 5)</h2>
    <p style="margin:0 0 14px 0;font-size:0.84rem;color:var(--muted);">
      Leave blank to submit anonymously. If you provide an email or phone number, PGH 311 requires a Last Name (defaults to "Resident" if left blank).
    </p>
    <form id="contactForm">
      <div class="form-grid">
        <div class="field">
          <label for="cFirstName">First Name (optional)</label>
          <input id="cFirstName" placeholder="John" />
        </div>
        <div class="field">
          <label for="cLastName">Last Name (auto-fills "Resident" if needed)</label>
          <input id="cLastName" placeholder="Doe" />
        </div>
        <div class="field">
          <label for="cEmail">Email (optional)</label>
          <input id="cEmail" type="email" placeholder="you@example.com" />
        </div>
        <div class="field">
          <label for="cPhone">Phone Number (optional, 10 digits)</label>
          <input id="cPhone" placeholder="412-555-1234" />
        </div>
      </div>
      <div style="display:flex;gap:10px;">
        <button type="submit" class="btn-secondary">Save Default Contact Info</button>
        <button type="button" id="clearContactBtn" class="btn-danger">Clear (Report Anonymously)</button>
      </div>
    </form>
  </div>

  <div class="panel-form">
    <h2 style="margin-bottom:14px;">Save New / Update Recurring Spot Preset</h2>
    <form id="presetForm">
      <div class="form-grid">
        <div class="field">
          <label for="pKey">Preset ID (short slug, e.g. penn-bikelane)*</label>
          <input id="pKey" required placeholder="penn-bikelane" />
        </div>
        <div class="field">
          <label for="pLabel">Display Label*</label>
          <input id="pLabel" required placeholder="Penn Ave Bike Lane" />
        </div>
        <div class="field">
          <label for="pAddress">Pittsburgh Address*</label>
          <input id="pAddress" required placeholder="900 Penn Ave" />
        </div>
      </div>
      <div class="field" style="margin-bottom:14px;">
        <label for="pDesc">Issue Description*</label>
        <textarea id="pDesc" required placeholder="Vehicles parked in the bike lane blocking traffic."></textarea>
      </div>
      <div class="form-grid">
        <div class="field">
          <label for="pVehicle">Vehicle Description (optional, default N/A)</label>
          <input id="pVehicle" placeholder="N/A" />
        </div>
        <div class="field">
          <label for="pPlate">License Plate (optional)</label>
          <input id="pPlate" placeholder="Leave blank if unsure" />
        </div>
        <div class="field">
          <label for="pDay">Day of Week (default N/A)</label>
          <input id="pDay" placeholder="N/A" />
        </div>
        <div class="field">
          <label for="pTime">Time of Day (default N/A)</label>
          <input id="pTime" placeholder="N/A" />
        </div>
      </div>
      <div style="display:flex;gap:10px;">
        <button type="submit" class="btn-primary" style="flex:0 0 auto;padding:10px 20px;">Save Preset</button>
        <button type="button" id="submitAdhocBtn" class="btn-secondary">Submit Once Without Saving</button>
      </div>
    </form>
  </div>

<script>
let presets = {};
let contact = {};
let isBusy = false;

const photoInput = document.getElementById('photoInput');
const clearPhotoBtn = document.getElementById('clearPhotoBtn');
photoInput.addEventListener('change', () => {
  clearPhotoBtn.style.display = photoInput.files.length ? 'inline-block' : 'none';
});
clearPhotoBtn.addEventListener('click', () => {
  photoInput.value = '';
  clearPhotoBtn.style.display = 'none';
});

function setStatus(type, title, details) {
  const box = document.getElementById('statusBox');
  const t = document.getElementById('statusTitle');
  const d = document.getElementById('statusDetails');
  box.className = 'status-box active ' + (type || '');
  t.innerHTML = title;
  d.textContent = details || '';
}

function setBusy(busy) {
  isBusy = busy;
  document.querySelectorAll('button').forEach(b => {
    if (b.id !== 'clearPhotoBtn') b.disabled = busy;
  });
}

async function readSelectedPhoto() {
  if (!photoInput.files || !photoInput.files[0]) return null;
  const file = photoInput.files[0];
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const base64 = reader.result.split(',')[1];
      resolve({ name: file.name, data_base64: base64 });
    };
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

async function loadState() {
  const res = await fetch('/api/presets');
  const data = await res.json();
  presets = data.presets || {};
  contact = data.contact || {};
  document.getElementById('cFirstName').value = contact.first_name || '';
  document.getElementById('cLastName').value = contact.last_name || '';
  document.getElementById('cEmail').value = contact.email || '';
  document.getElementById('cPhone').value = contact.phone || '';
  renderPresets();
}

function renderPresets() {
  const grid = document.getElementById('presetsGrid');
  grid.innerHTML = '';
  const keys = Object.keys(presets);
  if (keys.length === 0) {
    grid.innerHTML = '<div style="color:var(--muted);">No saved presets yet. Add one below!</div>';
    return;
  }
  for (const key of keys) {
    const p = presets[key];
    const card = document.createElement('div');
    card.className = 'card';
    card.innerHTML = `
      <div class="card-header">
        <h3 class="card-title"></h3>
        <span class="card-key"></span>
      </div>
      <div class="card-address"></div>
      <div class="card-desc"></div>
      <div class="card-actions">
        <button class="btn-primary report-btn">Report Now</button>
        <button class="btn-secondary edit-btn" title="Load into form below">Edit</button>
        <button class="btn-danger delete-btn" title="Delete preset">✕</button>
      </div>
    `;
    card.querySelector('.card-title').textContent = p.label || key;
    card.querySelector('.card-key').textContent = key;
    card.querySelector('.card-address').textContent = p.address;
    card.querySelector('.card-desc').textContent = p.description;

    card.querySelector('.report-btn').addEventListener('click', () => triggerReport({ preset: key }));
    card.querySelector('.edit-btn').addEventListener('click', () => {
      document.getElementById('pKey').value = key;
      document.getElementById('pLabel').value = p.label || key;
      document.getElementById('pAddress').value = p.address || '';
      document.getElementById('pDesc').value = p.description || '';
      document.getElementById('pVehicle').value = p.vehicle && p.vehicle !== 'N/A' ? p.vehicle : '';
      document.getElementById('pPlate').value = p.plate || '';
      document.getElementById('pDay').value = p.day && p.day !== 'N/A' ? p.day : '';
      document.getElementById('pTime').value = p.time && p.time !== 'N/A' ? p.time : '';
      document.getElementById('pKey').focus();
    });
    card.querySelector('.delete-btn').addEventListener('click', async () => {
      if (!confirm(`Delete preset "${p.label || key}"?`)) return;
      await fetch(`/api/presets/${encodeURIComponent(key)}`, { method: 'DELETE' });
      await loadState();
    });
    grid.appendChild(card);
  }
}

async function triggerReport(payload) {
  if (isBusy) return;
  const dryRun = document.getElementById('dryRunToggle').checked;
  const photo = await readSelectedPhoto();
  const body = { ...payload, dry_run: dryRun };
  if (photo) body.photo = photo;

  setBusy(true);
  const targetName = payload.preset ? (presets[payload.preset]?.label || payload.preset) : payload.address;
  setStatus(
    '',
    `<span class="spinner"></span> Submitting PGH 311 report for "${targetName}"...`,
    'Launching headless Chrome, matching GIS address, and filling steps 1-5 (~20-30s)...'
  );

  try {
    const res = await fetch('/api/report', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok || data.status === 'error') {
      throw new Error(data.error || 'Submission failed');
    }
    if (data.status === 'submitted') {
      setStatus(
        'success',
        `Submitted! Confirmation ID: ${data.service_request_id}`,
        `Matched Address : ${data.matched_address}\\nDescription     : ${data.description}` +
        (data.expected_resolution_date ? `\\nEst. Resolution : ${data.expected_resolution_date}` : '')
      );
    } else {
      setStatus(
        'success',
        `Dry-Run Complete (Stopped at Step 5 Review)`,
        `Matched Address : ${data.matched_address}\\nDescription     : ${data.description}` +
        (data.contact ? `\\nContact         : ${[data.contact.first_name, data.contact.last_name, data.contact.email, data.contact.phone].filter(Boolean).join(' | ')}` : '\\nContact         : Anonymous') +
        (data.photos_uploaded?.length ? `\\nPhotos Uploaded : ${data.photos_uploaded.join(', ')}` : '')
      );
    }
  } catch (err) {
    setStatus('error', 'Error Submitting Report', err.message || String(err));
  } finally {
    setBusy(false);
  }
}

document.getElementById('contactForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const payload = {
    first_name: document.getElementById('cFirstName').value.trim() || null,
    last_name: document.getElementById('cLastName').value.trim() || null,
    email: document.getElementById('cEmail').value.trim() || null,
    phone: document.getElementById('cPhone').value.trim() || null,
  };
  await fetch('/api/contact', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  await loadState();
  setStatus('success', 'Saved Default Contact Profile', JSON.stringify(payload, null, 2));
});

document.getElementById('clearContactBtn').addEventListener('click', async () => {
  const payload = { first_name: null, last_name: null, email: null, phone: null };
  await fetch('/api/contact', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  await loadState();
  setStatus('success', 'Cleared Default Contact Profile (Anonymous Mode)', '');
});

document.getElementById('presetForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const key = document.getElementById('pKey').value.trim();
  if (!key) return;
  const payload = {
    key,
    label: document.getElementById('pLabel').value.trim() || key,
    address: document.getElementById('pAddress').value.trim(),
    description: document.getElementById('pDesc').value.trim(),
    vehicle: document.getElementById('pVehicle').value.trim() || 'N/A',
    plate: document.getElementById('pPlate').value.trim() || null,
    day: document.getElementById('pDay').value.trim() || 'N/A',
    time: document.getElementById('pTime').value.trim() || 'N/A',
  };
  await fetch('/api/presets', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  await loadState();
  setStatus('success', `Saved preset "${payload.label}" (${key})`, `CLI usage: ./report_parking.py --preset ${key}`);
});

document.getElementById('submitAdhocBtn').addEventListener('click', () => {
  const address = document.getElementById('pAddress').value.trim();
  const description = document.getElementById('pDesc').value.trim();
  if (!address || !description) {
    alert('Please enter both an Address and Issue Description.');
    return;
  }
  triggerReport({
    address,
    description,
    vehicle: document.getElementById('pVehicle').value.trim() || 'N/A',
    plate: document.getElementById('pPlate').value.trim() || null,
    day: document.getElementById('pDay').value.trim() || 'N/A',
    time: document.getElementById('pTime').value.trim() || 'N/A',
  });
});

loadState();
</script>
</body>
</html>
"""


def run_web_server(
    host: str = "127.0.0.1",
    port: int = 8311,
    presets_path: Path = DEFAULT_PRESETS_PATH,
    open_browser: bool = False,
) -> None:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write(f"[web] {self.address_string()} - {fmt % args}\n")

        def _send_json(self, data: Any, status: int = HTTPStatus.OK) -> None:
            body = json.dumps(data, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            return json.loads(raw.decode("utf-8"))

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                body = WEB_UI_HTML.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/api/presets":
                self._send_json(load_config(presets_path))
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/contact":
                data = self._read_json()
                contact_data = {
                    "first_name": (data.get("first_name") or "").strip() or None,
                    "last_name": (data.get("last_name") or "").strip() or None,
                    "email": (data.get("email") or "").strip() or None,
                    "phone": (data.get("phone") or "").strip() or None,
                }
                save_contact_config(contact_data, presets_path)
                self._send_json({"status": "ok", "contact": contact_data})
                return

            if parsed.path == "/api/presets":
                data = self._read_json()
                key = str(data.get("key", "")).strip()
                address = str(data.get("address", "")).strip()
                description = str(data.get("description", "")).strip()
                if not key or not address or not description:
                    self._send_json(
                        {"status": "error", "error": "key, address, and description are required"},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                    return
                full_cfg = load_config(presets_path)
                presets = full_cfg["presets"]
                presets[key] = {
                    "label": str(data.get("label") or key).strip(),
                    "address": address,
                    "description": description,
                    "report_type": data.get("report_type") or "Illegal Parking",
                    "day": data.get("day") or "N/A",
                    "time": data.get("time") or "N/A",
                    "has_plate": "Yes" if data.get("plate") else (data.get("has_plate") or "Unsure"),
                    "plate": data.get("plate") or None,
                    "vehicle": data.get("vehicle") or "N/A",
                    "duration": data.get("duration") or "N/A",
                    "additional_info": data.get("additional_info") or None,
                    "location_details": data.get("location_details") or None,
                }
                save_presets_config(presets, presets_path)
                self._send_json({"status": "ok", "presets": presets})
                return

            if parsed.path == "/api/report":
                data = self._read_json()
                preset_key = data.get("preset")
                full_cfg = load_config(presets_path)
                presets = full_cfg["presets"]
                contact_cfg = full_cfg["contact"]
                if preset_key:
                    if preset_key not in presets:
                        self._send_json(
                            {"status": "error", "error": f"Unknown preset: {preset_key}"},
                            status=HTTPStatus.BAD_REQUEST,
                        )
                        return
                    cfg = dict(presets[preset_key])
                else:
                    cfg = data

                temp_photo_path: str | None = None
                photo_paths: list[str] = []
                try:
                    photo_obj = data.get("photo")
                    if isinstance(photo_obj, dict) and photo_obj.get("data_base64"):
                        raw_bytes = base64.b64decode(photo_obj["data_base64"])
                        orig_name = os.path.basename(photo_obj.get("name") or "upload.jpg")
                        _, ext = os.path.splitext(orig_name)
                        ext = ext or ".jpg"
                        fd, temp_photo_path = tempfile.mkstemp(suffix=ext, prefix="pgh311_")
                        with os.fdopen(fd, "wb") as f:
                            f.write(raw_bytes)
                        photo_paths.append(temp_photo_path)

                    res = submit_parking_report(
                        address=cfg["address"],
                        description=cfg["description"],
                        photo_paths=photo_paths,
                        report_type=cfg.get("report_type") or "Illegal Parking",
                        day_of_week=cfg.get("day") or "N/A",
                        time_of_day=cfg.get("time") or "N/A",
                        has_plate=cfg.get("has_plate") or "Unsure",
                        plate_number=cfg.get("plate"),
                        vehicle_description=cfg.get("vehicle") or "N/A",
                        duration=cfg.get("duration") or "N/A",
                        additional_info=cfg.get("additional_info"),
                        location_details=cfg.get("location_details"),
                        first_name=cfg.get("first_name") or contact_cfg.get("first_name"),
                        last_name=cfg.get("last_name") or contact_cfg.get("last_name"),
                        email=cfg.get("email") or contact_cfg.get("email"),
                        phone=cfg.get("phone") or contact_cfg.get("phone"),
                        headless=True,
                        dry_run=bool(data.get("dry_run", False)),
                        verbose=True,
                    )
                    self._send_json(res)
                except Exception as exc:
                    self._send_json(
                        {"status": "error", "error": str(exc)},
                        status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                finally:
                    if temp_photo_path and os.path.exists(temp_photo_path):
                        try:
                            os.unlink(temp_photo_path)
                        except OSError:
                            pass
                return

            self.send_error(HTTPStatus.NOT_FOUND)

        def do_DELETE(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/presets/"):
                from urllib.parse import unquote

                key = unquote(parsed.path[len("/api/presets/") :])
                full_cfg = load_config(presets_path)
                presets = full_cfg["presets"]
                if key in presets:
                    del presets[key]
                    save_presets_config(presets, presets_path)
                self._send_json({"status": "ok", "presets": presets})
                return
            self.send_error(HTTPStatus.NOT_FOUND)

    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}"
    print(f"PGH 311 One-Click Web UI running at {url}", file=sys.stderr)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server.", file=sys.stderr)
        server.server_close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Report illegal parking to Pittsburgh 311 (https://pittsburghpa.my.site.com/311/s/)."
    )
    parser.add_argument(
        "address",
        nargs="?",
        default=None,
        help="Street address or intersection in Pittsburgh (or omit when using --preset / --serve).",
    )
    parser.add_argument(
        "description",
        nargs="?",
        default=None,
        help="Short description of the illegal parking issue.",
    )
    parser.add_argument(
        "-P",
        "--preset",
        default=None,
        help="Submit using a saved preset name from presets.json.",
    )
    parser.add_argument(
        "--list-presets",
        action="store_true",
        help="List all saved presets and default contact info from presets.json and exit.",
    )
    parser.add_argument(
        "--save-preset",
        default=None,
        metavar="NAME",
        help="Save the provided address/description/options as a named preset in presets.json.",
    )
    parser.add_argument(
        "--preset-label",
        default=None,
        help="Optional human-readable display label when using --save-preset.",
    )
    parser.add_argument(
        "--delete-preset",
        default=None,
        metavar="NAME",
        help="Delete a named preset from presets.json and exit.",
    )
    parser.add_argument(
        "--save-contact",
        action="store_true",
        help="Save --email, --phone, --first-name, --last-name as the default contact profile in presets.json.",
    )
    parser.add_argument(
        "--clear-contact",
        action="store_true",
        help="Clear the saved default contact profile in presets.json (revert to anonymous).",
    )
    parser.add_argument(
        "--anonymous",
        action="store_true",
        help="Ignore any saved contact profile for this run and submit anonymously.",
    )
    parser.add_argument(
        "--first-name",
        default=None,
        help="Step 4 Contact: First name.",
    )
    parser.add_argument(
        "--last-name",
        default=None,
        help="Step 4 Contact: Last name (auto-fills 'Resident' if email/phone is set without last name).",
    )
    parser.add_argument(
        "--email",
        default=None,
        help="Step 4 Contact: Email address.",
    )
    parser.add_argument(
        "--phone",
        default=None,
        help="Step 4 Contact: 10-digit US phone number (e.g. '412-555-1234').",
    )
    parser.add_argument(
        "--presets-file",
        default=str(DEFAULT_PRESETS_PATH),
        help=f"Path to presets JSON file (default: {DEFAULT_PRESETS_PATH}).",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Start the local one-click Web UI server.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8311,
        help="Port for --serve Web UI (default: 8311).",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host for --serve Web UI (default: 127.0.0.1).",
    )
    parser.add_argument(
        "-p",
        "--photo",
        action="append",
        dest="photos",
        default=[],
        help="Optional path to a photo/attachment to upload (can be specified multiple times).",
    )
    parser.add_argument(
        "--report-type",
        choices=["Illegal Parking", "Boat or Trailer Parked on Street"],
        default=None,
        help="Question 1: Type of violation (default: 'Illegal Parking').",
    )
    parser.add_argument(
        "--day",
        default=None,
        help="Question 3: Day of the week when issue occurs (default: 'N/A').",
    )
    parser.add_argument(
        "--time",
        default=None,
        help="Question 4: Time of day/night when issue occurs (default: 'N/A').",
    )
    parser.add_argument(
        "--has-plate",
        choices=["Yes", "No", "Unsure"],
        default=None,
        help="Question 5: Does the vehicle have a license plate? (default: 'Unsure').",
    )
    parser.add_argument(
        "--plate",
        default=None,
        help="License plate number (automatically sets --has-plate to 'Yes').",
    )
    parser.add_argument(
        "--vehicle",
        default=None,
        help="Question 6: Vehicle/boat/trailer description (default: 'N/A').",
    )
    parser.add_argument(
        "--duration",
        default=None,
        help="Question 7: How long the vehicle has been in place (default: 'N/A').",
    )
    parser.add_argument(
        "--additional-info",
        default=None,
        help="Optional 'Additional Information' field on the Details step.",
    )
    parser.add_argument(
        "--location-details",
        default=None,
        help="Optional 'Additional Information' field on the Location step.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fill out all steps through the final Review screen without clicking Submit.",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run browser in visible (non-headless) mode.",
    )
    parser.add_argument(
        "--screenshot",
        default=None,
        help="Optional file path to save a screenshot of the final Review or Confirmation screen.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output result as JSON.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress logs on stderr.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    presets_path = Path(args.presets_file).expanduser().resolve()

    if args.serve:
        run_web_server(host=args.host, port=args.port, presets_path=presets_path)
        return 0

    full_cfg = load_config(presets_path)
    presets = full_cfg["presets"]
    contact_cfg = full_cfg["contact"]

    if args.clear_contact:
        local_path = save_contact_config(
            {
                "first_name": None,
                "last_name": None,
                "email": None,
                "phone": None,
            },
            presets_path,
        )
        print(f"Cleared saved contact profile in {local_path}.")
        return 0

    if args.save_contact:
        new_contact = {
            "first_name": args.first_name if args.first_name is not None else contact_cfg.get("first_name"),
            "last_name": args.last_name if args.last_name is not None else contact_cfg.get("last_name"),
            "email": args.email if args.email is not None else contact_cfg.get("email"),
            "phone": args.phone if args.phone is not None else contact_cfg.get("phone"),
        }
        local_path = save_contact_config(new_contact, presets_path)
        contact_cfg = new_contact
        print(f"Saved default contact profile to {local_path} (gitignored):")
        print(f"  First Name : {contact_cfg.get('first_name') or '(none)'}")
        print(f"  Last Name  : {contact_cfg.get('last_name') or '(auto: Resident)'}")
        print(f"  Email      : {contact_cfg.get('email') or '(none)'}")
        print(f"  Phone      : {contact_cfg.get('phone') or '(none)'}")
        if not args.preset and not args.address:
            return 0

    if args.list_presets:
        if args.json:
            print(json.dumps(full_cfg, indent=2))
        else:
            has_contact = any(contact_cfg.values())
            local_path = get_local_presets_path(presets_path)
            print(f"Default Contact ({local_path}):")
            if has_contact:
                print(
                    f"  Name: {contact_cfg.get('first_name') or ''} {contact_cfg.get('last_name') or '(auto: Resident)'}".strip()
                )
                print(f"  Email: {contact_cfg.get('email') or '(none)'} | Phone: {contact_cfg.get('phone') or '(none)'}")
            else:
                print("  Anonymous (all contact fields blank)")
            print()
            if not presets:
                print("No presets saved.")
            else:
                print(f"Saved Spot Presets ({presets_path}):")
                for k, v in presets.items():
                    label = v.get("label") or k
                    print(f"  {k:<18} | {label} — {v.get('address')}")
                    print(f"  {'':<18}   \"{v.get('description')}\"")
        return 0

    if args.delete_preset:
        if args.delete_preset not in presets:
            print(f"Error: Preset '{args.delete_preset}' not found.", file=sys.stderr)
            return 1
        del presets[args.delete_preset]
        save_presets_config(presets, presets_path)
        print(f"Deleted preset '{args.delete_preset}'.")
        return 0

    # Resolve fields from preset + CLI overrides
    preset_cfg: dict[str, Any] = {}
    if args.preset:
        if args.preset not in presets:
            available = ", ".join(presets.keys()) or "none"
            print(
                f"Error: Unknown preset '{args.preset}'. Available presets: {available}",
                file=sys.stderr,
            )
            return 1
        preset_cfg = presets[args.preset]

    address = args.address or preset_cfg.get("address")
    description = args.description or preset_cfg.get("description")
    report_type = args.report_type or preset_cfg.get("report_type") or "Illegal Parking"
    day = args.day or preset_cfg.get("day") or "N/A"
    time_of_day = args.time or preset_cfg.get("time") or "N/A"
    has_plate = args.has_plate or preset_cfg.get("has_plate") or "Unsure"
    plate = args.plate if args.plate is not None else preset_cfg.get("plate")
    vehicle = args.vehicle or preset_cfg.get("vehicle") or "N/A"
    duration = args.duration or preset_cfg.get("duration") or "N/A"
    additional_info = (
        args.additional_info
        if args.additional_info is not None
        else preset_cfg.get("additional_info")
    )
    location_details = (
        args.location_details
        if args.location_details is not None
        else preset_cfg.get("location_details")
    )

    if args.anonymous:
        first_name = last_name = email = phone = None
    else:
        first_name = args.first_name or preset_cfg.get("first_name") or contact_cfg.get("first_name")
        last_name = args.last_name or preset_cfg.get("last_name") or contact_cfg.get("last_name")
        email = args.email or preset_cfg.get("email") or contact_cfg.get("email")
        phone = args.phone or preset_cfg.get("phone") or contact_cfg.get("phone")

    if args.save_preset:
        if not address or not description:
            print(
                "Error: Both address and description are required when using --save-preset.",
                file=sys.stderr,
            )
            return 1
        presets[args.save_preset] = {
            "label": args.preset_label or preset_cfg.get("label") or args.save_preset,
            "address": address,
            "description": description,
            "report_type": report_type,
            "day": day,
            "time": time_of_day,
            "has_plate": "Yes" if plate else has_plate,
            "plate": plate,
            "vehicle": vehicle,
            "duration": duration,
            "additional_info": additional_info,
            "location_details": location_details,
        }
        save_presets_config(presets, presets_path)
        print(f"Saved preset '{args.save_preset}' to {presets_path}.")
        if not args.preset and not args.dry_run:
            return 0

    if not address or not description:
        print(
            "Error: Please provide <address> and <description>, or use --preset <name>, or --serve.",
            file=sys.stderr,
        )
        return 1

    try:
        res = submit_parking_report(
            address=address,
            description=description,
            photo_paths=args.photos,
            report_type=report_type,
            day_of_week=day,
            time_of_day=time_of_day,
            has_plate=has_plate,
            plate_number=plate,
            vehicle_description=vehicle,
            duration=duration,
            additional_info=additional_info,
            location_details=location_details,
            first_name=first_name,
            last_name=last_name,
            email=email,
            phone=phone,
            headless=not args.headed,
            dry_run=args.dry_run,
            screenshot_path=args.screenshot,
            verbose=not args.quiet,
        )
    except Exception as exc:
        if args.json:
            print(json.dumps({"status": "error", "error": str(exc)}, indent=2))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(res, indent=2))
    else:
        if res["status"] == "submitted":
            print("Report Submitted Successfully!")
            print(f"  Confirmation ID : {res['service_request_id']}")
            print(f"  Matched Address : {res['matched_address']}")
            if res.get("expected_resolution_date"):
                print(f"  Est. Resolution : {res['expected_resolution_date']}")
            if res.get("screenshot"):
                print(f"  Screenshot      : {res['screenshot']}")
        else:
            print("Dry Run Complete (Stopped at Step 5 of 5: Review)")
            print(f"  Matched Address : {res['matched_address']}")
            print(f"  Description     : {res['description']}")
            if res.get("contact"):
                c = res["contact"]
                print(
                    f"  Contact         : {' '.join(filter(None, [c.get('first_name'), c.get('last_name')]))} | "
                    f"Email: {c.get('email') or 'none'} | Phone: {c.get('phone') or 'none'}"
                )
            if res.get("photos_uploaded"):
                print(f"  Photos Uploaded : {', '.join(res['photos_uploaded'])}")
            if res.get("screenshot"):
                print(f"  Screenshot      : {res['screenshot']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
