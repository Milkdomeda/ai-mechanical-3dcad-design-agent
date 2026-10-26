from pathlib import Path

import pytest

from mechanical_design_agent.analysis_types import AnalysisError
from test_fea_gui_input import INPUT


def files(tmp_path, text=INPUT):
    path = tmp_path / 'model.inp'
    path.write_text(text, encoding='utf-8')
    return path


def test_round_trip_ignores_ids_but_not_physics(tmp_path):
    from mechanical_design_agent.analysis_deck import inspect_native_deck, compare_deck_semantics
    first = inspect_native_deck(files(tmp_path), allowed_root=tmp_path)
    second = inspect_native_deck(files(tmp_path, INPUT.replace('200000,.3', '2e5,0.30')), allowed_root=tmp_path)
    assert compare_deck_semantics(first, second)['equivalent'] is True
    changed = inspect_native_deck(files(tmp_path, INPUT.replace('5,3,-20', '5,3,20')), allowed_root=tmp_path)
    assert compare_deck_semantics(first, changed)['equivalent'] is False
    assert first['force_N'] == ['0', '0', '-40']
    assert first['moment_Nmm'] == ['-10', '10', '0']


def test_bijective_ids_sets_and_material_names_have_same_physics(tmp_path):
    from mechanical_design_agent.analysis_deck import inspect_native_deck, compare_deck_semantics
    first = inspect_native_deck(files(tmp_path), allowed_root=tmp_path)
    lines, section = [], ''
    for line in INPUT.splitlines():
        if line.startswith('*'): section = line.split(',')[0]
        elif section in ('*NODE', '*NSET', '*CLOAD', '*ELEMENT'):
            cells = line.split(',')
            if section in ('*NODE', '*CLOAD'): cells[0] = str(int(cells[0])+100)
            else: cells = [str(int(value)+100) for value in cells]
            line = ','.join(cells)
        lines.append(line.replace('FIXED', 'CLAMP').replace('VOLUME', 'CELLS').replace('BODY', 'DOMAIN').replace('STEEL', 'MAT'))
    second = inspect_native_deck(files(tmp_path, '\n'.join(lines)), allowed_root=tmp_path)
    assert compare_deck_semantics(first, second)['equivalent'] is True


def test_native_include_is_scoped_and_hashed(tmp_path):
    from mechanical_design_agent.analysis_deck import inspect_native_deck
    content = tmp_path / 'mesh.inp'
    before, after = INPUT.split('*MATERIAL', 1)
    content.write_text(before, encoding='utf-8')
    main = files(tmp_path, '*INCLUDE, INPUT=mesh.inp\n*MATERIAL' + after)
    inspection = inspect_native_deck(main, allowed_root=tmp_path)
    assert {item['relative_path'] for item in inspection['files']} == {'model.inp', 'mesh.inp'}
    content.write_text(before.replace('5,.5,0,0', '5,.51,0,0'), encoding='utf-8')
    changed = inspect_native_deck(main, allowed_root=tmp_path)
    assert inspection['semantic_sha256'] != changed['semantic_sha256']


@pytest.mark.parametrize('include', ['../secret.inp', '/tmp/outside.inp', 'C:\\other.inp', '\\\\server\\share\\file.inp', 'https://example.com/file.inp'])
def test_rejects_include_escape(tmp_path, include):
    from mechanical_design_agent.analysis_deck import inspect_native_deck
    with pytest.raises(AnalysisError) as error:
        inspect_native_deck(files(tmp_path, '*INCLUDE,INPUT=' + include + '\n'), allowed_root=tmp_path)
    assert error.value.code == 'DECK_REFERENCE_UNSAFE'


@pytest.mark.parametrize('card', ['*EQUATION\n', '*SPRING\n', '*TRANSFORM,NSET=ALL\n', '*DYNAMIC\n', '*CLOAD,AMPLITUDE=HIDDEN\n'])
def test_no_stabilization_or_hidden_physics(tmp_path, card):
    from mechanical_design_agent.analysis_deck import inspect_native_deck
    with pytest.raises(AnalysisError):
        inspect_native_deck(files(tmp_path, INPUT.replace('*CLOAD\n', card)), allowed_root=tmp_path)


def test_rf_output_scope_is_explicit(tmp_path):
    from mechanical_design_agent.analysis_deck import inspect_native_deck
    totals = inspect_native_deck(files(tmp_path), allowed_root=tmp_path)
    nodal = inspect_native_deck(files(tmp_path, INPUT.replace('TOTALS=ONLY', 'TOTALS=YES')), allowed_root=tmp_path)
    assert totals['rf_output'] == 'totals_only'
    assert nodal['rf_output'] == 'nodal_and_totals'
    assert totals['semantic_sha256'] != nodal['semantic_sha256']


def test_gravity_only_is_valid_and_units_are_explicit(tmp_path):
    from mechanical_design_agent.analysis_deck import inspect_native_deck
    text = INPUT.replace('*EXPANSION,ZERO=0\n0', '*DENSITY\n1e-9').replace('*CLOAD\n5,3,-20\n7,3,-20', '*DLOAD\nBODY,GRAV,10000,0,0,-1')
    result = inspect_native_deck(files(tmp_path, text), allowed_root=tmp_path)
    assert result['distributed_loads'] == [['gravity', ['1'], ['0', '0', '-10000']]]
    assert result['unit_system'] == 'N-mm-s-tonne'


def test_include_cycle_and_duplicate_output_fail(tmp_path):
    from mechanical_design_agent.analysis_deck import inspect_native_deck
    for text in ('*INCLUDE,INPUT=model.inp\n', INPUT.replace('*END STEP', '*NODE FILE\nU\n*END STEP')):
        with pytest.raises(AnalysisError): inspect_native_deck(files(tmp_path, text), allowed_root=tmp_path)


def test_contract_density_uses_explicit_native_unit_bridge():
    from decimal import Decimal
    from mechanical_design_agent.resources.freecad.analysis_build import native_density
    number, unit = native_density('.000000001').split(' ')
    assert Decimal(number) == 1000 and unit == 'kg/m^3'


def test_repeated_includes_cannot_evade_expanded_budget(tmp_path):
    from mechanical_design_agent.analysis_deck import inspect_native_deck
    (tmp_path/'comment.inp').write_text('** comment only\n', encoding='utf-8')
    with pytest.raises(AnalysisError) as error:
        inspect_native_deck(files(tmp_path, '*INCLUDE,INPUT=comment.inp\n'*33 + INPUT), allowed_root=tmp_path)
    assert error.value.code == 'DECK_REFERENCE_UNSAFE'


def test_native_semantics_do_not_depend_on_callers_decimal_context(tmp_path):
    from decimal import localcontext
    from mechanical_design_agent.analysis_deck import inspect_native_deck
    source = files(tmp_path)
    with localcontext() as context:
        context.prec = 3
        low = inspect_native_deck(source, allowed_root=tmp_path)
    with localcontext() as context:
        context.prec = 64
        high = inspect_native_deck(source, allowed_root=tmp_path)
    assert low == high
