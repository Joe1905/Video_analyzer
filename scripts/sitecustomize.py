"""Python startup hook for NarratoAI container.
Patches Streamlit's Tornado server to mount the Modern Workbench SPA and APIs on port 8501.
"""
try:
    from app.services import narrato_workbench_server
    narrato_workbench_server.patch_streamlit_server()
except Exception as exc:
    import sys
    sys.stderr.write(f"sitecustomize: failed to patch Streamlit workbench: {exc}\n")
