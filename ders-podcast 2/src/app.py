import base64
import json
import os
import re
import time
import uuid

import boto3
from botocore.config import Config

REGION = os.environ["AWS_REGION"]
BUCKET = os.environ["BUCKET"]
TABLE = os.environ["TABLE"]
WORKER = os.environ["WORKER"]
DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "100"))

s3 = boto3.client(
    "s3",
    region_name=REGION,
    config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
)
table = boto3.resource("dynamodb").Table(TABLE)
lam = boto3.client("lambda")

with open(os.path.join(os.path.dirname(__file__), "index.html"), encoding="utf-8") as f:
    PAGE = f.read()

ID_RE = re.compile(r"^[a-f0-9]{32}$")


def reply(code, body, ctype="application/json"):
    if ctype == "application/json":
        body = json.dumps(body)
    return {
        "statusCode": code,
        "headers": {"Content-Type": ctype, "Cache-Control": "no-store"},
        "body": body,
    }


def read_body(event):
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def take_quota():
    day = time.strftime("%Y%m%d", time.gmtime())
    r = table.update_item(
        Key={"id": "quota#" + day},
        UpdateExpression="ADD n :one SET exp = :t",
        ExpressionAttributeValues={":one": 1, ":t": int(time.time()) + 172800},
        ReturnValues="UPDATED_NEW",
    )
    return int(r["Attributes"]["n"]) <= DAILY_LIMIT


def upload():
    if not take_quota():
        return reply(429, {"error": "Daily limit reached, please try again tomorrow."})
    job_id = uuid.uuid4().hex
    url = s3.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": BUCKET,
            "Key": "uploads/" + job_id + ".pdf",
            "ContentType": "application/pdf",
        },
        ExpiresIn=600,
    )
    return reply(200, {"id": job_id, "url": url})


def start(data):
    job_id = str(data.get("id", ""))
    if not ID_RE.match(job_id):
        return reply(400, {"error": "Invalid job id."})
    lang = data.get("lang") if data.get("lang") in ("tr", "en") else "tr"
    mode = "dialog" if data.get("mode") == "dialog" else "monolog"
    try:
        minutes = max(2, min(8, int(data.get("minutes", 5))))
    except (TypeError, ValueError):
        minutes = 5
    try:
        s3.head_object(Bucket=BUCKET, Key="uploads/" + job_id + ".pdf")
    except Exception:
        return reply(400, {"error": "The PDF was not uploaded."})
    try:
        table.put_item(
            Item={"id": job_id, "status": "processing", "exp": int(time.time()) + 86400},
            ConditionExpression="attribute_not_exists(id)",
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return reply(409, {"error": "This job has already been started."})
    lam.invoke(
        FunctionName=WORKER,
        InvocationType="Event",
        Payload=json.dumps(
            {"id": job_id, "lang": lang, "mode": mode, "minutes": minutes}
        ).encode(),
    )
    return reply(202, {"id": job_id})


def status(job_id):
    if not ID_RE.match(job_id):
        return reply(400, {"error": "Invalid job id."})
    item = table.get_item(Key={"id": job_id}).get("Item")
    if not item:
        return reply(404, {"error": "Job not found."})
    out = {"status": item["status"]}
    if item["status"] == "done":
        out["title"] = item.get("title", "")
        out["script"] = item.get("script", "")
        out["note"] = item.get("note", "")
        out["audio"] = s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": BUCKET,
                "Key": item["audioKey"],
                "ResponseContentType": "audio/mpeg",
            },
            ExpiresIn=3600,
        )
    if item["status"] == "error":
        out["error"] = item.get("msg", "Processing failed.")
    return reply(200, out)


def handler(event, context):
    http = event["requestContext"]["http"]
    method = http["method"]
    path = event.get("rawPath", "/")
    if method == "GET" and path == "/":
        return reply(200, PAGE, "text/html; charset=utf-8")
    try:
        if method == "POST" and path == "/api/upload":
            return upload()
        if method == "POST" and path == "/api/start":
            return start(read_body(event))
        if method == "GET" and path == "/api/status":
            params = event.get("queryStringParameters") or {}
            return status(params.get("id", ""))
    except Exception as e:
        print(repr(e))
        return reply(500, {"error": "Server error."})
    return reply(404, {"error": "Not found."})
