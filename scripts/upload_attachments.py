#!/usr/bin/env python3
"""upload_attachments.py - Async attachment upload for recruit-match-suite-fast.

Three-phase workflow (driven by --phase):

  phase prepare:
      Read candidates.json (resume) or jobs_draft.json (job) + records template
      to build a manifest of dws attachment-upload commands the agent must
      execute one-by-one. Writes attachment_manifest.json.

  phase upload:
      Read the dws_out_<seq>.json files (produced by the agent), PUT
      the local file bytes to each OSS uploadUrl in parallel, then assemble
      a record-update JSON that maps record_id -> real fileToken.
      Writes attachment_update_records.json and prints the dws record-update
      command for the agent to run.

  phase verify:
      Print a dws record-query command so the agent can verify attachments
      landed in the table.

Use --intake-type resume (default) or --intake-type job to select the
intake type. Resume reads candidates.json + upsert_records_template.json;
job reads jobs_draft.json + create_records_template.json.

Zero third-party dependencies; Python 3.9+ stdlib only.
"""

import argparse
import json
import mimetypes
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPT_DIR.parent
SHARED_DIR = str(PLUGIN_ROOT / "shared")
if SHARED_DIR not in sys.path:
    sys.path.insert(0, SHARED_DIR)

from performance_timing import (  # noqa: E402
    TIMING_FILE_NAME,
    append_event_and_observe,
    read_summary,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def unwrap_dws_json(payload):
    """兼容 raw dws JSON 与 {stdout, elapsed_ms, ...} 包装格式。"""
    if isinstance(payload, dict) and "stdout" in payload:
        stdout = payload.get("stdout")
        if isinstance(stdout, str):
            try:
                payload = json.loads(stdout)
            except ValueError:
                return {}
        elif isinstance(stdout, dict):
            payload = stdout
    return payload if isinstance(payload, dict) else {}


def read_timed_dws_result(path):
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"elapsed_ms": 0, "elapsed_measured": False}
    value = payload.get("elapsed_ms") if isinstance(payload, dict) else None
    measured = (not isinstance(value, bool)
                and isinstance(value, (int, float)) and value >= 0)
    return {"elapsed_ms": int(value) if measured else 0,
            "elapsed_measured": measured}


def attachment_dws_commands(out_dir):
    manifest_path = Path(out_dir) / "attachment_manifest.json"
    commands = []
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            commands = [{"seq": item.get("seq"), "command_type": "attachment upload"}
                        for item in (manifest.get("attachments") or [])]
        except (OSError, ValueError):
            commands = []
    commands.append({"seq": "update", "result_file": "dws_out_update.json",
                     "command_type": "record update"})
    return commands


def load_config(config_path, intake_type="resume"):
    """Return (base_id, table_id, attachment_field_id, name_field_id) from config.json.

    For resume: reads tables.resume, fields.resume.attachment, fields.resume.name
    For job: reads tables.job, fields.job.attachment, fields.job.job_name
    """
    cfg = load_json(config_path)
    base_id = cfg["base_id"]
    table_id = cfg["tables"][intake_type]["table_id"]
    attach_field_id = cfg["fields"][intake_type]["attachment"]
    # name field: resume uses "name", job uses "job_name"
    name_key = "name" if intake_type == "resume" else "job_name"
    name_field_id = cfg["fields"][intake_type][name_key]
    return base_id, table_id, attach_field_id, name_field_id


def guess_mime(file_path):
    mime, _ = mimetypes.guess_type(file_path)
    return mime or "application/octet-stream"


def find_input_file(file_name, source_dir):
    """Locate the input file on disk given its file_name."""
    # Direct match in source dir
    candidate = os.path.join(source_dir, file_name)
    if os.path.isfile(candidate):
        return candidate
    # Search recursively under source_dir
    for root, _dirs, files in os.walk(source_dir):
        if file_name in files:
            return os.path.join(root, file_name)
    return None


def oss_put(file_path, upload_url, mime_type):
    """PUT file bytes to OSS uploadUrl. Returns (ok, error_msg)."""
    try:
        with open(file_path, "rb") as fh:
            data = fh.read()
        req = urllib.request.Request(
            upload_url,
            data=data,
            method="PUT",
            headers={"Content-Type": mime_type},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            if 200 <= resp.status < 300:
                return True, None
            return False, "HTTP %d" % resp.status
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


# ---------------------------------------------------------------------------
# Phase 1: prepare
# ---------------------------------------------------------------------------

def phase_prepare(args):
    out_dir = args.out_dir
    config_path = args.config
    intake_type = args.intake_type
    base_id, table_id, attach_field_id, name_field_id = load_config(config_path, intake_type)

    # Determine file names and entry list key based on intake type
    if intake_type == "resume":
        draft_file = "candidates.json"
        template_file = "upsert_records_template.json"
        entries_key = "candidates"
        name_field = "name"  # field in draft entry
        source_subdirs = ["AI简历"]
    else:  # job
        draft_file = "jobs_draft.json"
        template_file = "create_records_template.json"
        entries_key = "jobs"
        name_field = "job_name"  # field in draft entry
        source_subdirs = ["JD", "岗位JD", "data/JD", "data/岗位JD"]

    draft_path = os.path.join(out_dir, draft_file)
    template_path = args.records_file or os.path.join(out_dir, template_file)

    if not os.path.isfile(draft_path):
        sys.stderr.write("ERROR: %s not found in %s\n" % (draft_file, out_dir))
        return 1
    if not os.path.isfile(template_path):
        sys.stderr.write("ERROR: %s not found in %s\n" % (template_file, out_dir))
        return 1

    draft_data = load_json(draft_path)
    records_data = load_json(template_path)

    # Determine the source directory for input files.
    # For resume: walk up looking for data/AI简历
    # For job: walk up looking for data/JD or similar; also check --files paths
    source_dir = None
    check = out_dir
    for _ in range(5):
        for sub in source_subdirs:
            trial = os.path.join(check, "data", sub)
            if os.path.isdir(trial):
                source_dir = trial
                break
        if source_dir:
            break
        # Also check check/sub directly (for data/JD case)
        for sub in source_subdirs:
            trial = os.path.join(check, sub)
            if os.path.isdir(trial):
                source_dir = trial
                break
        if source_dir:
            break
        parent = os.path.dirname(check)
        if parent == check:
            break
        check = parent
    if source_dir is None:
        # Fall back: use --files parent directory if available
        if args.files:
            file_parents = set(os.path.dirname(os.path.abspath(f)) for f in args.files)
            if len(file_parents) == 1:
                source_dir = file_parents.pop()
        if source_dir is None:
            source_dir = out_dir

    attachments = []
    for entry in draft_data.get(entries_key, []):
        file_name = entry.get("file_name", "")
        entry_key = entry.get("key", "")
        name = entry.get(name_field, "")

        # Find the matching record to get the fake token
        # Match by name field from config
        matched_record = None
        for rec in records_data:
            cells = rec.get("cells", {})
            if cells.get(name_field_id) == name:
                matched_record = rec
                break

        if not matched_record:
            sys.stderr.write(
                "WARNING: no record match for %s %s (%s)\n"
                % (intake_type, entry_key, name)
            )
            continue

        cells = matched_record.get("cells", {})
        attach = cells.get(attach_field_id, [])
        if not attach:
            sys.stderr.write(
                "WARNING: no fake token for %s %s (%s)\n"
                % (intake_type, entry_key, name)
            )
            continue
        fake_token = attach[0].get("fileToken", "")

        # The seq number is embedded in the fake token: emit_fake_token_<seq>
        try:
            seq = int(fake_token.rsplit("_", 1)[-1])
        except (ValueError, IndexError):
            sys.stderr.write(
                "WARNING: cannot parse seq from fake token '%s' for %s\n"
                % (fake_token, entry_key)
            )
            continue

        # Find the file on disk
        file_path = find_input_file(file_name, source_dir)
        if not file_path and args.files:
            # Try matching against --files paths
            for f in args.files:
                if os.path.basename(f) == file_name:
                    file_path = f
                    break
        if not file_path:
            sys.stderr.write(
                "WARNING: file not found: %s (searched in %s)\n" % (file_name, source_dir)
            )
            # Still include it with file_path=None; agent can fix path manually
            file_path = ""

        size = os.path.getsize(file_path) if file_path and os.path.isfile(file_path) else 0
        mime = guess_mime(file_path) if file_path else "application/octet-stream"

        dws_cmd = (
            "dws aitable attachment upload"
            " --base-id %s"
            " --file-name '%s'"
            " --size %d"
            " --mime-type %s"
            " --format json"
            " --yes"
            " --timeout 180"
        ) % (base_id, file_name, size, mime)

        attachments.append({
            "seq": seq,
            "record_id": entry.get("record_id") or "",  # may be filled later
            "entry_key": entry_key,
            "entry_name": name,
            "file_path": file_path,
            "file_name": file_name,
            "size": size,
            "mime_type": mime,
            "dws_cmd": dws_cmd,
        })

    # Sort by seq
    attachments.sort(key=lambda a: a["seq"])

    manifest = {"attachments": attachments}
    manifest_path = os.path.join(out_dir, "attachment_manifest.json")
    write_json(manifest_path, manifest)

    # Print dws commands to stdout
    for att in attachments:
        sys.stdout.write(
            "SEQ|%d|FILE|%s|CMD|%s\n"
            % (att["seq"], att["file_path"], att["dws_cmd"])
        )
    sys.stdout.flush()

    # Print instructions to stderr
    sys.stderr.write(
        "\nAgent: 请逐条执行以上 %d 条 dws 命令，每条命令的结果"
        "（system-reminder 中的 content 字段 JSON）写入 %s/dws_out_<seq>.json\n"
        "完成后运行: python3 upload_attachments.py --phase upload"
        " --out-dir %s --config %s\n"
        % (len(attachments), out_dir, out_dir, config_path)
    )
    sys.stderr.flush()
    return 0


# ---------------------------------------------------------------------------
# Phase 2: upload
# ---------------------------------------------------------------------------

def phase_upload(args):
    out_dir = args.out_dir
    config_path = args.config
    intake_type = args.intake_type
    base_id, table_id, attach_field_id, _ = load_config(config_path, intake_type)

    manifest_path = os.path.join(out_dir, "attachment_manifest.json")
    if not os.path.isfile(manifest_path):
        sys.stderr.write("ERROR: attachment_manifest.json not found in %s\n" % out_dir)
        return 1

    manifest = load_json(manifest_path)
    attachments = manifest["attachments"]

    # Validate that all dws_out_<seq>.json files exist and have fileToken
    missing = []
    token_map = {}  # seq -> (record_id_hint, fileToken, uploadUrl, file_path, mime)

    for att in attachments:
        seq = att["seq"]
        dws_out_path = os.path.join(out_dir, "dws_out_%d.json" % seq)
        if not os.path.isfile(dws_out_path):
            missing.append(seq)
            continue
        try:
            dws_result = load_json(dws_out_path)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write("ERROR reading dws_out_%d.json: %s\n" % (seq, exc))
            missing.append(seq)
            continue

        dws_result = unwrap_dws_json(dws_result)
        data = dws_result.get("data", dws_result)
        file_token = data.get("fileToken")
        upload_url = data.get("uploadUrl")
        if not file_token or not upload_url:
            missing.append(seq)
            continue

        token_map[seq] = {
            "record_id_hint": att.get("record_id", ""),
            "entry_key": att.get("entry_key", ""),
            "entry_name": att.get("entry_name", ""),
            "file_token": file_token,
            "upload_url": upload_url,
            "file_path": att.get("file_path", ""),
            "file_name": att.get("file_name", ""),
            "mime_type": att.get("mime_type", "application/octet-stream"),
        }

    if missing:
        sys.stderr.write(
            "ERROR: %d dws_attach_out file(s) missing or lacking fileToken: %s\n"
            % (len(missing), ", ".join(str(s) for s in missing))
        )
        return 1

    # PUT files to OSS in parallel (concurrency=5)
    sys.stderr.write("Uploading %d files to OSS (concurrency=5)...\n" % len(token_map))
    upload_results = {}  # seq -> (ok, error)

    def _upload(seq_info):
        seq, info = seq_info
        ok, err = oss_put(info["file_path"], info["upload_url"], info["mime_type"])
        return seq, ok, err

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {
            pool.submit(_upload, (seq, info)): seq
            for seq, info in token_map.items()
        }
        for fut in as_completed(futures):
            seq, ok, err = fut.result()
            upload_results[seq] = (ok, err)
            status = "OK" if ok else "FAIL: " + (err or "unknown")
            sys.stderr.write(
                "  seq %d (%s) -> %s\n"
                % (seq, token_map[seq]["file_name"], status)
            )

    failed = [s for s, (ok, _) in upload_results.items() if not ok]
    if failed:
        sys.stderr.write(
            "\nERROR: %d OSS upload(s) failed: %s\n"
            % (len(failed), ", ".join(str(s) for s in sorted(failed)))
        )

    # Build record update JSON.
    # We need record_ids. The candidates.json record_id field is currently null,
    # so we cannot build the update records here without them.
    # The script will check for record_id in the manifest; if missing, it will
    # print a warning and produce the update file with record_id placeholders,
    # plus instructions for the agent to fill them in.
    update_records = []
    missing_record_ids = []
    for seq in sorted(token_map.keys()):
        info = token_map[seq]
        record_id = info["record_id_hint"]
        if not record_id:
            # Try to get from manifest attachment entry
            for att in attachments:
                if att["seq"] == seq:
                    record_id = att.get("record_id", "")
                    break
        if not record_id:
            missing_record_ids.append(seq)
            record_id = "RECORD_ID_FOR_%s" % info["entry_key"]

        update_records.append({
            "recordId": record_id,
            "cells": {
                attach_field_id: [{"fileToken": info["file_token"]}]
            },
        })

    update_path = os.path.join(out_dir, "attachment_update_records.json")
    write_json(update_path, update_records)

    if missing_record_ids:
        sys.stderr.write(
            "\nWARNING: %d record(s) have no record_id (seqs: %s).\n"
            "The attachment_update_records.json has placeholder recordIds.\n"
            "Please fill in real record IDs before running the dws update command.\n"
            % (len(missing_record_ids), ", ".join(str(s) for s in missing_record_ids))
        )

    # Print dws command to stdout
    dws_cmd = (
        "dws aitable record update"
        " --base-id %s"
        " --table-id %s"
        " --records-file %s"
        " --format json"
        " --yes"
        " --timeout 180"
    ) % (base_id, table_id, update_path)
    sys.stdout.write(dws_cmd + "\n")
    sys.stdout.flush()

    # Print instructions to stderr
    sys.stderr.write(
        "\nAgent: 请执行以上 dws record update 命令，"
        "结果写入 %s/dws_out_update.json\n"
        "完成后运行: python3 upload_attachments.py --phase verify"
        " --out-dir %s --config %s\n"
        % (out_dir, out_dir, config_path)
    )
    sys.stderr.flush()
    return 0 if not failed else 2


# ---------------------------------------------------------------------------
# Phase 3: verify
# ---------------------------------------------------------------------------

def phase_verify(args):
    out_dir = args.out_dir
    config_path = args.config
    intake_type = args.intake_type
    base_id, table_id, attach_field_id, _ = load_config(config_path, intake_type)

    manifest_path = os.path.join(out_dir, "attachment_manifest.json")
    if not os.path.isfile(manifest_path):
        sys.stderr.write("ERROR: attachment_manifest.json not found in %s\n" % out_dir)
        return 1

    manifest = load_json(manifest_path)
    record_ids = [a.get("record_id", "") for a in manifest["attachments"] if a.get("record_id")]
    record_ids_str = ",".join(record_ids) if record_ids else "<record_ids>"

    dws_cmd = (
        "dws aitable record query"
        " --base-id %s"
        " --table-id %s"
        " --record-ids %s"
        " --field-ids %s"
        " --format json"
        " --yes"
        " --timeout 180"
    ) % (base_id, table_id, record_ids_str, attach_field_id)

    sys.stdout.write(dws_cmd + "\n")
    sys.stdout.flush()

    count = len(manifest["attachments"])
    label = "简历" if intake_type == "resume" else "岗位"
    sys.stderr.write(
        "\nAgent: 请执行以上命令验证附件是否已写入。或直接在 AI 表格中查看。\n"
        "%d 份%s已入库，附件补传流程完成。\n" % (count, label)
    )
    sys.stderr.flush()
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Async attachment upload for recruit-match-suite-fast"
    )
    parser.add_argument(
        "--phase",
        required=True,
        choices=["prepare", "upload", "verify"],
        help="Workflow phase to execute",
    )
    parser.add_argument(
        "--intake-type",
        default="resume",
        choices=["resume", "job"],
        help="Intake type: resume (default) or job",
    )
    parser.add_argument(
        "--config",
        help="Path to config.json",
    )
    parser.add_argument(
        "--out-dir",
        required=True,
        help="Output directory (same as intake output)",
    )
    parser.add_argument(
        "--files",
        nargs="*",
        default=None,
        help="Glob of input files (shell-expanded by caller)",
    )
    parser.add_argument(
        "--records-file",
        default=None,
        help="Path to records template file (defaults to <out-dir>/upsert_records_template.json for resume, create_records_template.json for job)",
    )
    args = parser.parse_args()
    started_at_ms = int(time.time() * 1000)

    if args.phase == "prepare":
        if not args.config:
            parser.error("--config is required for --phase prepare")
        rc = phase_prepare(args)
    elif args.phase == "upload":
        if not args.config:
            parser.error("--config is required for --phase upload")
        rc = phase_upload(args)
    elif args.phase == "verify":
        if not args.config:
            parser.error("--config is required for --phase verify")
        rc = phase_verify(args)
    else:
        rc = 0

    finished_at_ms = int(time.time() * 1000)
    append_event_and_observe(
        args.out_dir,
        {"name": "upload_attachments.%s" % args.phase,
         "category": "orchestrator_local",
         "started_at_ms": started_at_ms,
         "finished_at_ms": finished_at_ms,
         "metadata": {"returncode": rc, "intake_type": args.intake_type}},
        attachment_dws_commands(args.out_dir),
        read_timed_dws_result,
        scope_complete=False,
        scope="attachment_workflow_partial",
    )
    timing_path = Path(args.out_dir).resolve() / TIMING_FILE_NAME
    sys.stderr.write("PERFORMANCE:%s\n" % timing_path)
    sys.stderr.write("[performance] %s\n" % json.dumps(read_summary(args.out_dir),
                                                        ensure_ascii=False))
    sys.stderr.flush()
    return rc


if __name__ == "__main__":
    sys.exit(main())
