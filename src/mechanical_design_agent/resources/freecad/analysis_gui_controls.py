"""Select live native task widgets; hidden stale dialogs are never operated."""


def visible_child(root, kind, name):
    candidates = root.findChildren(kind,name)
    current = [widget for widget in candidates if widget.isVisible() and widget.isEnabled()]
    if len(current) != 1:
        raise ValueError('native control must be uniquely visible and enabled: '+name+'; candidates='+str([(widget.isVisible(),widget.isEnabled()) for widget in candidates]))
    return current[0]


def named_ancestor(control, name):
    current = control.parentWidget()
    while current is not None:
        if current.objectName() == name: return current
        current = current.parentWidget()
    raise ValueError('native control is outside its expected task panel: '+name)
