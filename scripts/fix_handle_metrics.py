"""Script to fix handle_video_metrics in scripts/web_app.py."""
from pathlib import Path

target = Path(__file__).parent / "web_app.py"
text = target.read_text(encoding="utf-8")

bad_block = """        except (json.JSONDecodeError, ValueError) as exc:
        with download_jobs_lock:
            download_jobs[job.id] = job
        thread = threading.Thread(target=run_download_job, args=(job.id,), daemon=True)
        thread.start()
        return json_response(self, HTTPStatus.ACCEPTED, public_download_job(job))"""

good_block = """        except (json.JSONDecodeError, ValueError) as exc:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        job = MetricsJob(
            id=str(uuid.uuid4()),
            target=target,
            endpoint=endpoint,
        )
        with metrics_jobs_lock:
            metrics_jobs[job.id] = job
        thread = threading.Thread(target=run_metrics_job, args=(job.id,), daemon=True)
        thread.start()
        return json_response(self, HTTPStatus.ACCEPTED, public_metrics_job(job))"""

if bad_block in text:
    text = text.replace(bad_block, good_block, 1)
    target.write_text(text, encoding="utf-8")
    print("Successfully fixed handle_video_metrics in web_app.py!")
else:
    print("bad_block not found in web_app.py.")
