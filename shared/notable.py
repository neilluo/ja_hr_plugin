#!/usr/bin/env python3
"""钉钉 AI 表格（Notable）OpenAPI 客户端。

唯一传输层：token 缓存、指数退避重试、附件三步上传、记录 CRUD、字段值类型转换。
所有脚本只 import 本模块访问远端，禁止 subprocess 调 dws / 直接 urllib 调钉钉。

用法:
    from notable import Notable
    nt = Notable()                      # 读插件根 config.json + .secrets.json
    rows = nt.list_records("resume")    # 业务表名: resume/job/match/perm
    ids = nt.create_records("resume", [{"name": "张三", ...}])   # 业务键入参
"""

import json
import mimetypes
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = "https://api.dingtalk.com"
CACHE = os.path.join(ROOT, ".dingtalk_token_cache.json")
RETRY_STATUS = {429, 500, 503}
RETRY_CODES = {"invalidRequest.document.stillInitializing"}


class NotableError(Exception):
    """远端调用失败（已含错误码与 message）。"""


def _load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _creds():
    key = os.environ.get("DINGTALK_APP_KEY", "")
    sec = os.environ.get("DINGTALK_APP_SECRET", "")
    if key and sec:
        return key, sec
    p = os.path.join(ROOT, ".secrets.json")
    if os.path.exists(p):
        d = _load_json(p)
        if d.get("app_key") and d.get("app_secret"):
            return d["app_key"], d["app_secret"]
    raise NotableError("缺少凭证：设置环境变量 DINGTALK_APP_KEY/SECRET 或创建 .secrets.json")


class Notable:
    def __init__(self, config_path=None):
        self.cfg = _load_json(config_path or os.path.join(ROOT, "config.json"))
        self.base = self.cfg["base_id"]
        self.op = self.cfg["operator_id"]
        self._token = None

    # ── 传输 ──────────────────────────────────────────────
    def token(self, force=False):
        if self._token and not force:
            return self._token
        if not force and os.path.exists(CACHE):
            try:
                c = _load_json(CACHE)
                if time.time() < c["expire_at"] - 300:
                    self._token = c["accessToken"]
                    return self._token
            except (KeyError, ValueError, OSError):
                pass
        key, sec = _creds()
        body = json.dumps({"appKey": key, "appSecret": sec}).encode()
        req = urllib.request.Request(
            API + "/v1.0/oauth2/accessToken", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read())
        self._token = d["accessToken"]
        tmp = CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"accessToken": self._token,
                       "expire_at": time.time() + d.get("expireIn", 7200)}, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, CACHE)
        return self._token

    def call(self, method, path, body=None, raw=None, retries=3):
        """调钉钉 OpenAPI。path 不含 operatorId，自动追加。raw=bytes 时裸请求（OSS PUT）。"""
        sep = "&" if "?" in path else "?"
        url = API + path + ("" if raw is not None else sep + "operatorId=" + self.op)
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        headers = {"Content-Type": "application/json"} if raw is None else {"Content-Type": "application/octet-stream"}
        if raw is None:
            headers["x-acs-dingtalk-access-token"] = self.token()
        for attempt in range(retries + 1):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as e:
                payload = e.read()
                try:
                    err = json.loads(payload or b"{}")
                except ValueError:
                    err = {"message": payload[:200].decode("utf-8", "replace")}
                retryable = e.code in RETRY_STATUS or err.get("code") in RETRY_CODES
                if e.code == 401 and attempt == 0:
                    self.token(force=True)
                    headers["x-acs-dingtalk-access-token"] = self._token
                    retryable = True
                if retryable and attempt < retries:
                    time.sleep(1.5 * 2 ** attempt)
                    continue
                raise NotableError("HTTP %s %s: %s" % (e.code, err.get("code", ""), err.get("message", "")))
            except urllib.error.URLError as e:
                if attempt < retries:
                    time.sleep(1.5 * 2 ** attempt)
                    continue
                raise NotableError("网络错误 %s: %s" % (path, e.reason))
        raise NotableError("重试耗尽: " + path)

    def put(self, url, raw, mime, retries=3):
        """裸 PUT（OSS 直传），带指数退避重试；失败抛 NotableError。"""
        for attempt in range(retries + 1):
            req = urllib.request.Request(url, data=raw,
                                         headers={"Content-Type": mime}, method="PUT")
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    if r.status in (200, 201, 204):
                        return
                    raise NotableError("OSS PUT 失败: HTTP %s" % r.status)
            except urllib.error.HTTPError as e:
                if attempt < retries and e.code in RETRY_STATUS:
                    time.sleep(1.5 * 2 ** attempt)
                    continue
                raise NotableError("OSS PUT 失败: HTTP %s" % e.code)
            except urllib.error.URLError as e:
                if attempt < retries:
                    time.sleep(1.5 * 2 ** attempt)
                    continue
                raise NotableError("OSS PUT 网络错误: %s" % e.reason)
        raise NotableError("OSS PUT 重试耗尽")

    def map_parallel(self, fn, items, workers=5):
        """并发映射（I/O 密集：附件上传等）。结果保持输入顺序；单项异常收集不炸批。
        返回 (results, errors)，errors = [(下标, 异常)]。"""
        results, errors = [None] * len(items), []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(fn, item): i for i, item in enumerate(items)}
            for fut in futs:
                i = futs[fut]
                try:
                    results[i] = fut.result()
                except Exception as e:  # noqa: BLE001 单项失败不阻断批次
                    errors.append((i, e))
        return results, errors

    # ── 记录 CRUD（业务表名/业务键入参，中文字段名出参）──────
    def sheet(self, table):
        return self.cfg["tables"][table]["table_id"]

    def cn(self, table, biz):
        try:
            return self.cfg["fields"][table][biz]
        except KeyError:
            valid = ", ".join(sorted(self.cfg["fields"].get(table, {})))
            raise NotableError("未知业务键 '%s'（表 %s）。可用: %s" % (biz, table, valid))

    def list_records(self, table, flt=None, biz_fields=None, limit=0):
        """全量分页拉取。返回 [{id, fields:{业务键: 原始值}}]。flt = {业务键: 值} 等值过滤。"""
        sheet, out, token = self.sheet(table), [], ""
        names = [self.cn(table, k) for k in (biz_fields or [])]
        while True:
            body = {"maxResults": 100}
            if token:
                body["nextToken"] = token
            if names:
                body["fieldIdOrNames"] = names
            if flt:
                body["filter"] = {"combination": "and", "conditions": [
                    {"field": self.cn(table, k), "operator": "equal", "value": [v]}
                    for k, v in flt.items()]}
            r = self.call("POST", "/v1.0/notable/bases/%s/sheets/%s/records/list" % (self.base, sheet), body)
            for rec in r.get("records", []):
                out.append({"id": rec["id"],
                            "fields": {self._biz(table, k): self._norm(v, self._type(table, k))
                                       for k, v in rec.get("fields", {}).items()}})
            if limit and len(out) >= limit:
                return out[:limit]
            token = r.get("nextToken", "")
            if not r.get("hasMore") or not token:
                return out

    @staticmethod
    def _norm(v, ftype=None):
        """读回值归一：select {id,name}→name；multipleSelect→[name]；number 字符串→float。"""
        if isinstance(v, dict):
            return v.get("name", v)
        if isinstance(v, list) and v and all(isinstance(x, dict) and "name" in x for x in v):
            return [x["name"] for x in v]
        if ftype == "number" and isinstance(v, str):
            try:
                return float(v)
            except ValueError:
                return v
        return v

    def _type(self, table, cn_name):
        for biz, cn in self.cfg["fields"][table].items():
            if cn == cn_name:
                return self.cfg["types"][table].get(biz)
        return None

    def _biz(self, table, cn_name):
        m = self.cfg["fields"][table]
        for biz, cn in m.items():
            if cn == cn_name:
                return biz
        return cn_name

    def create_records(self, table, rows):
        """rows: [{业务键: 值}]。返回新建 record id 列表（顺序不保证，需回读确认）。"""
        sheet = self.sheet(table)
        ids = []
        for i in range(0, len(rows), 10):
            chunk = [{"fields": self._cells(table, row)} for row in rows[i:i + 10]]
            r = self.call("POST", "/v1.0/notable/bases/%s/sheets/%s/records" % (self.base, sheet),
                          {"records": chunk})
            ids += [v["id"] for v in r.get("value", [])]
        return ids

    def update_records(self, table, rows):
        """rows: [{id, 业务键: 值}]。"""
        sheet = self.sheet(table)
        for i in range(0, len(rows), 10):
            chunk = [{"id": row["id"], "fields": self._cells(table, {k: v for k, v in row.items() if k != "id"})}
                     for row in rows[i:i + 10]]
            self.call("PUT", "/v1.0/notable/bases/%s/sheets/%s/records" % (self.base, sheet),
                      {"records": chunk})

    def delete_records(self, table, ids):
        sheet = self.sheet(table)
        for i in range(0, len(ids), 50):
            self.call("POST", "/v1.0/notable/bases/%s/sheets/%s/records/delete" % (self.base, sheet),
                      {"recordIds": ids[i:i + 50]})

    # ── 字段值转换 ────────────────────────────────────────
    def _cells(self, table, row):
        types = self.cfg["types"][table]
        cells = {}
        for biz, val in row.items():
            if val is None or val == "" or val == []:
                continue
            cast = self._cast(val, self.cfg["types"][table].get(biz, "text"))
            if cast is not None:
                cells[self.cn(table, biz)] = cast
        return cells

    @staticmethod
    def _cast(val, t):
        """转成 OpenAPI 写入格式；非法值返回 None（_cells 会跳过），不抛异常打断批次。"""
        if t == "number":
            try:
                return round(float(val), 4)
            except (TypeError, ValueError):
                return None
        if t == "date":
            if isinstance(val, (int, float)):
                return int(val if val > 1e11 else val * 1000)
            try:
                return int(time.mktime(time.strptime(str(val)[:10], "%Y-%m-%d")) * 1000)
            except ValueError:
                return None
        if t == "multipleSelect":
            return val if isinstance(val, list) else [str(val)]
        if t == "attachment":
            return val if isinstance(val, list) else [val]
        return str(val)

    # ── 附件三步上传 ──────────────────────────────────────
    def upload_attachment(self, path):
        """本地文件 → 附件 cell 值。query 上传地址 → 裸 PUT OSS → 返回 cell dict。"""
        size = os.path.getsize(path)
        name = os.path.basename(path)
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        r = self.call("POST", "/v1.0/doc/docs/resources/%s/uploadInfos/query" % self.base,
                      {"size": size, "mediaType": mime, "resourceName": name})
        res = r.get("result", r)
        with open(path, "rb") as f:
            raw = f.read()
        self.put(res["uploadUrl"], raw, mime)
        return {"filename": name, "size": size, "type": mime,
                "url": res["resourceUrl"], "resourceId": res["resourceId"]}
