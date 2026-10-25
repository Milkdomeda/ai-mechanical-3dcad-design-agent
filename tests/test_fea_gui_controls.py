import pytest


class Widget:
    def __init__(self,visible=True,enabled=True): self.visible,self.enabled=visible,enabled
    def isVisible(self): return self.visible
    def isEnabled(self): return self.enabled


class Root:
    def __init__(self,children): self.children=children
    def findChildren(self,kind,name): return self.children.get(name,[])


def test_hidden_stale_controls_cannot_replace_the_active_result_panel():
    from fea_gui_controls import visible_child
    stale,current=Widget(False,False),Widget()
    root=Root({'field':[stale,current]})
    assert visible_child(root,Widget,'field') is current


@pytest.mark.parametrize('children', [[],[Widget(False)],[Widget(True,False)],[Widget(),Widget()]])
def test_missing_disabled_or_ambiguous_controls_fail_closed(children):
    from fea_gui_controls import visible_child
    with pytest.raises(ValueError): visible_child(Root({'field':children}),Widget,'field')


def test_panel_is_the_selected_controls_ancestor_not_an_unrelated_same_named_widget():
    from fea_gui_controls import named_ancestor
    class Node:
        def __init__(self,name,parent=None): self.name,self.parent=name,parent
        def objectName(self): return self.name
        def parentWidget(self): return self.parent
    active=Node('ShowDisplacement')
    button=Node('rb_x_displacement',Node('group',active))
    assert named_ancestor(button,'ShowDisplacement') is active
    with pytest.raises(ValueError): named_ancestor(button,'WrongPanel')
