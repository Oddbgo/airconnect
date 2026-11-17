# PM2.5 Air Alert - Team AirConnect (CS446/IT332)
# Every hour: read Bangkok PM2.5 -> save to DynamoDB -> email if too high -> update web page in S3
import json, os, html, urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import boto3
from boto3.dynamodb.conditions import Key

TABLE_NAME = "pm25-history"      # DynamoDB table name
BUCKET_PREFIX = "pm25-page"      # S3 bucket name starts with this
TOPIC_NAME = "pm25-alerts"       # SNS topic name
STATION_ID = "50t"               # Air4Thai station: Chulalongkorn Hospital, Bangkok
PM25_LIMIT = 37.5                # Thai PM2.5 standard (ug/m3, 24-hour average)
BKK = timezone(timedelta(hours=7))

s3 = boto3.client("s3")
sns = boto3.client("sns")
table = boto3.resource("dynamodb").Table(TABLE_NAME)


def find_bucket():
    for b in s3.list_buckets()["Buckets"]:
        if b["Name"].startswith(BUCKET_PREFIX):
            return b["Name"]
    raise Exception("No S3 bucket starts with " + BUCKET_PREFIX)


def find_topic():
    for t in sns.list_topics()["Topics"]:
        if t["TopicArn"].endswith(":" + TOPIC_NAME):
            return t["TopicArn"]
    raise Exception("No SNS topic named " + TOPIC_NAME)


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "pm25-air-alert"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def from_air4thai():
    d = get_json("https://air4thai.pcd.go.th/services/getNewAQI_JSON.php?stationID=" + STATION_ID)
    last = d["AQILast"]
    pm = float(last["PM25"]["value"])
    yesterday = (datetime.now(BKK) - timedelta(days=1)).strftime("%Y-%m-%d")
    if pm < 0 or last["date"] < yesterday:
        raise Exception("Air4Thai has no fresh PM2.5 value")
    return pm, d["nameEN"], last["date"] + " " + last["time"], "Air4Thai"


def from_open_meteo():
    d = get_json("https://air-quality-api.open-meteo.com/v1/air-quality"
                 "?latitude=13.73&longitude=100.54&current=pm2_5&timezone=Asia%2FBangkok")
    return float(d["current"]["pm2_5"]), "Bangkok (Pathum Wan)", d["current"]["time"].replace("T", " "), "Open-Meteo"


def level(pm):
    for top, color, thai in [(15, "#5cc8f5", "ดีมาก"), (25, "#8fd16a", "ดี"),
                             (37.5, "#ffd84d", "ปานกลาง"), (75, "#ffa64d", "เริ่มมีผลต่อสุขภาพ")]:
        if pm <= top:
            return color, thai
    return "#ff5c5c", "มีผลต่อสุขภาพ"


def make_page(latest, rows):
    color, thai = level(latest["pm25"])
    trs = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            html.escape(str(r["checked_at"])), r["pm25"], html.escape(str(r.get("status", ""))))
        for r in rows)
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="300"><title>PM2.5 Air Alert</title>
<style>body{{font-family:sans-serif;max-width:560px;margin:auto;padding:16px;background:#f4f6f8;color:#1b2430}}
.card{{background:{color};border-radius:16px;padding:24px;text-align:center}}
.big{{font-size:64px;font-weight:bold}}table{{width:100%;border-collapse:collapse;background:#fff}}
td,th{{padding:6px;border-bottom:1px solid #ddd;text-align:left}}</style></head><body>
<h1>PM2.5 Air Alert &middot; Bangkok</h1>
<div class="card"><div class="big">{latest['pm25']}</div><div>&micro;g/m&sup3; &middot; {thai}</div>
<div>{html.escape(latest['place'])} &middot; measured {html.escape(latest['measured_at'])}</div></div>
<p>Alert limit: {latest['limit']} &micro;g/m&sup3; &middot; Source: {latest['source']}<br>
Last check: {latest['checked_at']} (Bangkok time)</p>
<h2>Last 24 checks</h2><table><tr><th>Checked at</th><th>PM2.5</th><th>Status</th></tr>{trs}</table>
<p><small>Team AirConnect &middot; CS446 &middot; updated every hour by AWS Lambda</small></p>
</body></html>"""


def lambda_handler(event, context):
    event = event if isinstance(event, dict) else {}
    limit = float(event.get("test_limit", PM25_LIMIT))   # test_limit is only for testing the email

    try:
        pm, place, measured, source = from_air4thai()
    except Exception as e:
        print("Air4Thai failed, using Open-Meteo instead:", e)
        pm, place, measured, source = from_open_meteo()

    checked = datetime.now(BKK).strftime("%Y-%m-%d %H:%M:%S")
    status = "UNHEALTHY" if pm > limit else "OK"

    # 1) save history in DynamoDB
    table.put_item(Item={"station": STATION_ID, "checked_at": checked, "pm25": Decimal(str(pm)),
                         "place": place, "measured_at": measured, "source": source, "status": status})

    # 2) email alert with SNS
    if pm > limit:
        sns.publish(TopicArn=find_topic(),
                    Subject=f"PM2.5 Alert: {pm} ug/m3 in Bangkok",
                    Message=f"PM2.5 is {pm} ug/m3 at {place} ({measured}).\n"
                            f"Our limit is {limit} ug/m3. Please wear an N95 mask and stay indoors if you can.\n"
                            f"Data source: {source}")

    # 3) update the web page in S3
    rows = table.query(KeyConditionExpression=Key("station").eq(STATION_ID),
                       ScanIndexForward=False, Limit=24)["Items"]
    latest = {"pm25": pm, "place": place, "measured_at": measured, "checked_at": checked,
              "source": source, "status": status, "limit": limit}
    bucket = find_bucket()
    s3.put_object(Bucket=bucket, Key="latest.json", Body=json.dumps(latest),
                  ContentType="application/json", CacheControl="no-cache")
    s3.put_object(Bucket=bucket, Key="index.html", Body=make_page(latest, rows).encode("utf-8"),
                  ContentType="text/html; charset=utf-8", CacheControl="no-cache")

    print(latest)
    return latest
