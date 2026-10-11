"""Narrow UI recovery controls, independent of document/layer edits."""
from .. import ops as b
from ..compat import QtWidgets


def dismiss_recovery_dialog(params):
    dialog = QtWidgets.QApplication.activeModalWidget()
    if dialog is None:
        return {"dismissed": False, "reason": "no_modal_dialog"}
    # Use native Qt metadata, not translated title/button text. Never reject
    # a save-changes, file chooser, overwrite or other unrelated modal dialog.
    name = dialog.metaObject().className()
    if not isinstance(dialog, QtWidgets.QDialog) or not dialog.inherits("KisAutoSaveRecoveryDialog"):
        raise b.OpError("The active modal is {0}, not Krita's autosave Recover Files dialog. "
                        "It was left open.".format(name), kind="unsupported_dialog")
    # KisApplication.checkAutosaveFiles handles Rejected by clearing only its
    # recovery list. Accept/Discard All can delete autosaves; never invoke them.
    dialog.reject()
    return {"dismissed": True, "dialog_class": name, "action": "cancel",
            "autosave_files": "kept", "documents_edited": False}


OPERATIONS = [("dismiss_recovery_dialog", dismiss_recovery_dialog, 10, False)]
