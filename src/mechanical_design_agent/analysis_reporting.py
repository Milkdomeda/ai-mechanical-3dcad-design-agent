"""Deterministic report renderer; numerical decisions come from validation."""
import html
import json
import re

from .analysis_protocol import record_sha256


def _text(value):
    text=html.escape(str(value)).replace('\r',' ').replace('\n',' ')
    return re.sub(r'([\\`*_\[\]()#!|])',r'\\\1',text)


def _json(value):return json.dumps(value,ensure_ascii=True,allow_nan=False)


def build_report(spec,result,receipts):
    attempt=receipts['attempt'];numerical=attempt['numerical'];criteria=attempt['criteria']
    lines=['# Linear static FEA report','',
           '**This is not an engineering certification, manufacturing release, or standards approval.**','',
           'Numerical status: `'+result['numerical_status']+'`. Engineering criteria: `'+result['criteria_status']+'`. Applicability: `'+result['applicability_status']+'`.','',
           'Currentness at publication: `'+result['currentness']+'`. A historical snapshot does not establish the state of a changed CAD design; query the current analysis status before reuse.','',
           'Design: `'+spec['design_id']+'`; analysis: `'+spec['analysis_id']+'`; run: `'+result['run_id']+'`.','',
           'Approved specification SHA-256: `'+record_sha256(spec,schema_name='spec')+'`.','',
           'Source FCStd SHA-256: `'+spec['source']['sha256']+'`; safe BREP SHA-256: `'+spec['source']['geometry_sha256']+'`.','',
           'Units: N, mm, s, tonne, MPa. Coordinate system: global. Static step 1, increment 1, time 1.','',
           'Stress recovery: `native_extrapolated_averaged_nodal`. Native result display uses the verified undeformed mesh, effective deformation scale 0.','',
           '## Declared physical model','',
           'The model uses one bonded, connected C3D10 solid, linear elasticity and infinitesimal deformation. Supports and loads follow the approved physical regions. No contact, buckling, creep, fatigue, plasticity or printed interlayer strength is inferred.','']
    material=spec['material']
    lines.extend(['Material declarations: E = '+material['youngs_modulus']['value']+' MPa; nu = '+material['poisson_ratio']['value']+'. Density: '+_text(_json(material['density']))+'.','',
                  'Frame mass basis: '+_text(_json(spec['frame_mass']))+'.','',
                  'Excluded reference objects: '+', '.join('`'+name+'`' for name in spec['source']['excluded_object_names'])+'.',''])
    for case in spec['cases']:
        lines.extend(['Case `'+case['case_id']+'`: '+_text(case['operating_condition'])+'; load interpretation `'+case['load_meaning']+'`; fixed regions '+', '.join('`'+name+'`' for name in case['fixed_regions'])+'.','',
                      'Frozen load declarations: '+_text(_json(case['loads']))+'.',''])
    for text in spec.get('assumptions',[]):lines.extend(['- '+_text(text),''])
    lines.extend(['## Converged observations and global diagnostics','',
                  'A fixed engineering ROI is distinct from the raw global maximum. Constraint and load singularities remain visible in the global diagnostics; they are not silently removed.',''])
    for level in numerical['finest']:
        case=level['case_id'];mesh=level['mesh']
        lines.extend(['### Case `'+case+'`','',
                      'Finest mesh level '+str(level['level'])+': '+str(mesh['node_count'])+' nodes, '+str(mesh['element_count'])+' C3D10 elements; declared size '+mesh['size_mm']+' mm.','',
                      '| Observable | Region | Value | Unit | Native node | Position [mm] |','| --- | --- | ---: | --- | ---: | --- |'])
        for obs in level['observables']:
            location=obs['location']
            lines.append('| '+obs['id']+' | '+obs['region_id']+' | '+_json(obs['value'])+' | '+obs['unit']+' | '+str(location['node_id'])+' | '+_json(location['coordinates_mm'])+' |')
        lines.extend(['','Raw global peaks: '+_json(level['raw_global_peaks'])+'.',''])
        if 'raw_global_peak_locations' in level:lines.extend(['Raw peak positions and native IDs: '+_json(level['raw_global_peak_locations'])+'.',''])
        for check in level['checks']:
            lines.extend(['- `'+check['id']+'`: `'+check['status']+'`; residual '+_json(check['residual'])+' '+check['unit']+', allowed '+_json(check['tolerance'])+' '+check['unit']+'.',''])
        for limitation in level['applicability']['limitations']:lines.extend(['- '+_text(limitation),''])
        for image in receipts.get('images',{}).get(case,[]):lines.extend(['!['+case+' native field]('+image+')',''])
    lines.extend(['## Convergence and engineering criteria','',
                  'Each mandatory observation requires the last two adjacent mesh comparisons to pass. Displacement convergence cannot stand in for stress convergence.',''])
    for comparison in numerical['comparisons']:
        lines.extend(['- `'+comparison['case_id']+' / '+comparison['observable_id']+'`, levels '+str(comparison['from_level'])+' to '+str(comparison['to_level'])+': difference '+_json(comparison['difference'])+' '+comparison['unit']+', allowed '+_json(comparison['tolerance'])+' '+comparison['unit']+'; passed = '+_json(comparison['passed'])+'.',''])
    if not criteria['checks']:lines.extend(['No engineering allowable has been evaluated. The result remains screening evidence.',''])
    for check in criteria['checks']:
        lines.extend(['- `'+check['case_id']+' / '+check['id']+'`: `'+check['status']+'`; actual '+_json(check['actual'])+' '+check['unit']+', declared limit '+_json(check['limit'])+' '+check['unit']+', declared absolute tolerance '+_json(check['absolute_tolerance'])+' '+check['unit']+'. '+_text(check['evidence']),''])
    lines.extend(['## Reproduction and review','',
                  'The manifest binds the approved specification, numerical attempt, native GUI evidence, figures and this report. Raw input, solver outputs, meshes and normalized fields are retained in the copied attempt evidence.','',
                  'A report or display-only revision reuses those exact solver artifacts. Changing geometry, material, supports, loads or numerical policy requires a new reviewed run.','',
                  'User acceptance is recorded separately against an exact manifest; it does not approve CAD design changes or publish engineering knowledge.',''])
    return '\n'.join(lines)
