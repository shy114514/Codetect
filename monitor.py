#!/usr/bin/env python3
import argparse
import random
import subprocess
import time
from pathlib import Path
from string import Formatter
from urllib.parse import quote
from urllib.request import urlopen

import yaml


def load_config(path):
    with Path(path).open("r", encoding="utf-8") as config_file:
        return yaml.safe_load(config_file)


def render_template(template, values, url_encode=False):
    rendered_values = {}
    for key, value in values.items():
        text = str(value)
        rendered_values[key] = quote(text, safe="") if url_encode else text
    return template.format(**rendered_values)


def template_fields(template):
    return {
        field_name
        for _, field_name, _, _ in Formatter().parse(template)
        if field_name
    }


def run_command(command):
    return subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        check=False,
    )


def send_notification(config, match):
    values = {
        "match": match,
        "title": config.get("title", ""),
        "body": render_template(config.get("body", ""), {"match": match}),
    }

    url = render_template(
        config["notify_url"],
        values,
        url_encode=bool(template_fields(config["notify_url"])),
    )

    with urlopen(url, timeout=config.get("notify_timeout_seconds", 10)) as response:
        response.read()


def next_delay(config):
    interval = float(config.get("interval_seconds", 300))
    jitter = float(config.get("jitter_seconds", 0))
    return max(1.0, interval + random.uniform(-jitter, jitter))


def execute_once(config, matched, debug=False) -> bool:
    result = run_command(config["command"])
    output = result.stdout + result.stderr
    match_text = config["match_text"]

    if debug:
        print(f"stdout: \n{result.stdout}" + "stderr: \n" + result.stderr)

    if match_text not in output:
        print("Fail.")
        return False
    
    if matched:
        print("Still Matched. No notification sent.")
        time.sleep(config["sleep_seconds"])
        return True

    send_notification(config, match_text)
    print("Notification sent.")
    time.sleep(config["sleep_seconds"])
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Run a command periodically and notify api.day.app on matching output."
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to the YAML config file. Defaults to config.yaml.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one command check and exit.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print command stdout after each run.",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    matched = False

    while True:
        started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{started_at}] Running command...")
        matched = execute_once(config, matched, args.debug)

        if args.once:
            break

        delay = next_delay(config)
        print(f"Next run in {delay:.1f} seconds.")
        time.sleep(delay)


if __name__ == "__main__":
    main()
