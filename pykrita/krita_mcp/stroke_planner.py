"""Version 1 geometry planner. Pure Python; never draws pixels or calls Krita.

Bezier subdivision uses a chord/control-polygon error, then each edge is
sampled uniformly in arc length. Flattened vertices (including corners) are
retained. Profiles are piecewise linear in cumulative normalized distance.
One plan always maps to one native stroke; >128 samples is rejected.
"""
import math


class PlanError(ValueError):
    pass


def finite(v, name, low=None, high=None):
    if type(v) not in (int, float) or not math.isfinite(v):
        raise PlanError(name + ' must be finite numeric')
    if low is not None and v < low or high is not None and v > high:
        raise PlanError(name + ' out of range')
    return float(v)


def point(p):
    if not isinstance(p, list) or len(p) != 2:
        raise PlanError('Coordinates must be [x,y]')
    return tuple(finite(v, 'coordinate', -100000, 100000) for v in p)


def distance(a, z):
    return math.hypot(z[0]-a[0], z[1]-a[1])


def bezier(a, b, c, d, tolerance, depth=0):
    polygon = distance(a,b) + distance(b,c) + distance(c,d)
    chord = distance(a,d)
    def deviation(p):
        return abs((d[0]-a[0])*(a[1]-p[1])-(a[0]-p[0])*(d[1]-a[1]))/chord if chord else distance(a,p)
    if polygon - chord <= tolerance and max(deviation(b),deviation(c)) <= tolerance:
        return [a, d]
    if depth >= 16:
        raise PlanError('Bezier subdivision budget exceeded')
    def mid(p,q): return ((p[0]+q[0])/2, (p[1]+q[1])/2)
    ab, bc, cd = mid(a,b), mid(b,c), mid(c,d)
    abc, bcd = mid(ab,bc), mid(bc,cd)
    m = mid(abc,bcd)
    left=bezier(a,ab,abc,m,tolerance,depth+1)
    right=bezier(m,bcd,cd,d,tolerance,depth+1)
    if len(left)+len(right)>2048:raise PlanError('Bezier vertex budget exceeded')
    return left[:-1] + right


def profile(raw):
    if not isinstance(raw, dict) or set(raw)!={'interpolation','samples'} or raw.get('interpolation') != 'linear':
        raise PlanError('pressure_profile requires linear interpolation')
    values = raw.get('samples')
    if not isinstance(values, list) or not 2 <= len(values) <= 32:
        raise PlanError('pressure_profile needs 2..32 samples')
    pairs = []
    for v in values:
        if not isinstance(v, dict) or set(v) != {'at','value'}:
            raise PlanError('Profile sample must be {at,value}')
        pairs.append((finite(v['at'],'at',0,1), finite(v['value'],'pressure',0,1)))
    if pairs[0][0] != 0 or pairs[-1][0] != 1 or any(z[0] <= a[0] for a,z in zip(pairs,pairs[1:])):
        raise PlanError('Profile must strictly increase from 0 to 1')
    return pairs


def plan(spec):
    if not isinstance(spec, dict) or type(spec.get('version')) is not int or spec.get('version') != 1:
        raise PlanError('Stroke specification version must be 1')
    required = {'version','stroke_id','target','preset','color','size','opacity','erase',
                'geometry','pressure_profile','timing','expected_bounds','limits'}
    if not required <= set(spec) or set(spec)-required-{'tilt'}:
        raise PlanError('Missing or unknown stroke specification fields')
    if not isinstance(spec['stroke_id'],str) or not 1 <= len(spec['stroke_id']) <= 128:
        raise PlanError('stroke_id must be 1..128 characters')
    if not isinstance(spec['preset'],str) or not spec['preset']:
        raise PlanError('Exact preset name required')
    finite(spec['size'],'size',.1,2048)
    finite(spec['opacity'],'opacity',0,1)
    if type(spec['erase']) is not bool: raise PlanError('erase must be boolean')
    color = spec['color']
    if not isinstance(color,dict) or set(color) != {'rgba','profile'} or color['profile'] != 'sRGB-elle-V2-srgbtrc.icc':
        raise PlanError('Explicit pilot sRGB profile required')
    if not isinstance(color['rgba'],list) or len(color['rgba']) != 4 or any(type(v) is not int or not 0 <= v <= 255 for v in color['rgba']):
        raise PlanError('RGBA requires four integers in 0..255')
    if spec.get('tilt',{'x':0,'y':0}) != {'x':0,'y':0}:
        raise PlanError('Nonzero tilt response is not calibrated in the pilot')
    target = spec['target']
    if not isinstance(target,dict) or set(target) != {'document','layer','scope'} or not isinstance(target['document'],str) or not target['document'].startswith('session:') or not isinstance(target['layer'],str) or not target['layer'].startswith('uuid:'):
        raise PlanError('Target requires session document, uuid layer and explicit scope')
    bounds = spec['expected_bounds']
    if not isinstance(bounds,dict) or set(bounds) != {'x','y','width','height'}:
        raise PlanError('expected_bounds must be {x,y,width,height}')
    for k,v in bounds.items(): finite(v,k,0 if k in ('width','height') else -100000,100000)
    limits = spec['limits']
    if not isinstance(limits,dict) or set(limits) != {'max_points','spacing','tolerance'}:
        raise PlanError('limits requires max_points, spacing, tolerance')
    if type(limits['max_points']) is not int or not 2 <= limits['max_points'] <= 128:
        raise PlanError('max_points must be integer 2..128')
    spacing = finite(limits['spacing'],'spacing',.1,2048)
    tolerance = finite(limits['tolerance'],'tolerance',.001,2)
    timing = spec['timing']
    if not isinstance(timing,dict) or set(timing) != {'duration_ms'}:
        raise PlanError('timing requires duration_ms (constant arc-length speed)')
    duration = finite(timing['duration_ms'],'duration_ms',.1,100000)
    pressure = profile(spec['pressure_profile'])
    geometry = spec['geometry']
    if not isinstance(geometry,dict): raise PlanError('geometry must be object')
    if geometry.get('kind') == 'polyline' and set(geometry) == {'kind','points'}:
        if not isinstance(geometry['points'],list) or not 2 <= len(geometry['points']) <= 128:
            raise PlanError('Polyline requires 2..128 vertices')
        vertices = [point(p) for p in geometry['points']]
    elif geometry.get('kind') == 'cubic' and set(geometry) == {'kind','segments'}:
        segments = geometry['segments']
        if not isinstance(segments,list) or not 1 <= len(segments) <= 16:
            raise PlanError('Cubic requires 1..16 connected segments')
        vertices = []
        for segment in segments:
            if not isinstance(segment,list) or len(segment) != 4: raise PlanError('Cubic needs four control points')
            pts = [point(p) for p in segment]
            if vertices and vertices[-1] != pts[0]: raise PlanError('Cubic segments must join exactly')
            flattened = bezier(*pts,tolerance)
            vertices += flattened[1:] if vertices else flattened
    else: raise PlanError('geometry must be polyline or cubic')
    clean = [vertices[0]]
    for p in vertices[1:]:
        if p != clean[-1]: clean.append(p)
    if len(clean) < 2: raise PlanError('Zero-length strokes are unsupported; use a short explicit path')
    samples = [clean[0]]
    for a,z in zip(clean,clean[1:]):
        count = max(1,math.ceil(distance(a,z)/spacing))
        if len(samples)+count > limits['max_points']: raise PlanError('Continuous stroke sample budget exceeded; increase spacing')
        samples += [((a[0]+(z[0]-a[0])*i/count),(a[1]+(z[1]-a[1])*i/count)) for i in range(1,count+1)]
        samples[-1] = z
    arcs = [0.]
    for a,z in zip(samples,samples[1:]): arcs.append(arcs[-1]+distance(a,z))
    total = arcs[-1]
    result = []
    for p,s in zip(samples,arcs):
        u = s/total
        for a,z in zip(pressure,pressure[1:]):
            if u <= z[0]:
                value = a[1]+(z[1]-a[1])*(u-a[0])/(z[0]-a[0]); break
        if not (bounds['x'] <= p[0] <= bounds['x']+bounds['width'] and bounds['y'] <= p[1] <= bounds['y']+bounds['height']):
            raise PlanError('Geometry leaves declared expected bounds')
        result.append({'x':p[0],'y':p[1],'pressure':value,'timestamp_ms':duration*u})
    return {'version':1,'stroke_id':spec['stroke_id'],'points':result,'arc_length':total,
            'point_count':len(result),'coordinate_space':'document_pixels',
            'timing_policy':'constant speed along flattened arc length',
            'deterministic_geometry':True,'pixel_exact_replay':False}
