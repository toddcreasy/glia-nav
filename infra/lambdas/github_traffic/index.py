import datetime
import json
import os
import urllib.request

import boto3

REPO = os.environ["GITHUB_REPO"]
BUCKET = os.environ["BUCKET"]

s3 = boto3.client("s3")


def fetch(path: str, token: str) -> dict | list:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/traffic/{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "glia-nav-traffic",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def summary(snapshot: dict, yesterday: str) -> str:
    views, clones = snapshot["views"], snapshot["clones"]
    day = next((d for d in views["views"] if d["timestamp"].startswith(yesterday)), None)
    lines = [
        f"github.com/{REPO}",
        "",
        f"Yesterday ({yesterday}): {day['count'] if day else 0} views, "
        f"{day['uniques'] if day else 0} unique visitors",
        f"Last 14 days: {views['count']} views, {views['uniques']} unique visitors",
        f"Last 14 days: {clones['count']} clones, {clones['uniques']} unique cloners "
        "(mostly CI and bots)",
        "",
        "Referrers, last 14 days:",
    ]
    referrers = snapshot["referrers"]
    lines += [f"  {r['referrer']}: {r['count']} views, {r['uniques']} unique" for r in referrers]
    lines += ["  none"] if not referrers else []
    lines += ["", "Pages, last 14 days:"]
    paths = snapshot["paths"]
    lines += [f"  {p['path']}: {p['count']} views, {p['uniques']} unique" for p in paths]
    lines += ["  none"] if not paths else []
    return "\n".join(lines)


def handler(event, context):
    secrets = boto3.client("secretsmanager")
    token = secrets.get_secret_value(SecretId=os.environ["TOKEN_SECRET_ARN"])["SecretString"]
    snapshot = {
        "views": fetch("views", token),
        "clones": fetch("clones", token),
        "referrers": fetch("popular/referrers", token),
        "paths": fetch("popular/paths", token),
    }
    today = datetime.datetime.now(datetime.UTC).date()
    s3.put_object(Bucket=BUCKET, Key=f"snapshots/{today}.json", Body=json.dumps(snapshot))

    # GitHub only keeps 14 days. Each run rewrites the days it still returns, so today's
    # partial count is corrected tomorrow and older days keep their final value.
    try:
        history = json.load(s3.get_object(Bucket=BUCKET, Key="history.json")["Body"])
    except s3.exceptions.NoSuchKey:
        history = {"views": {}, "clones": {}}
    for kind in ("views", "clones"):
        for day in snapshot[kind][kind]:
            date = day["timestamp"][:10]
            history[kind][date] = {"count": day["count"], "uniques": day["uniques"]}
    s3.put_object(Bucket=BUCKET, Key="history.json", Body=json.dumps(history, sort_keys=True))

    boto3.client("sns").publish(
        TopicArn=os.environ["TOPIC_ARN"],
        Subject=f"glia-nav repo traffic, {today}",
        Message=summary(snapshot, str(today - datetime.timedelta(days=1))),
    )
