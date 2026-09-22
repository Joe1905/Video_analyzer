"""Keep read-only CDP clients from racing the task's native dialog handler."""
def observe_dialogs(browser):
    for context in browser.contexts:
        context.on("dialog", lambda _dialog: None)


def handle_dialog(dialog, on_error=None):
    try:
        if dialog.type == "beforeunload":
            dialog.accept()
        else:
            dialog.dismiss()
    except Exception as exc:
        if on_error:
            try:
                on_error(exc)
            except Exception:
                pass


def own_page_dialogs(page):
    page.on("dialog", lambda dialog: handle_dialog(dialog))
