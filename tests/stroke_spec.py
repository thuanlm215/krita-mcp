"""Runnable version 1 pilot stroke specification constructor."""
def stroke_spec(target, geometry=None, size=12, color=None, stroke_id='calibration', erase=False):
    return {'version':1,'stroke_id':stroke_id,'target':target,'preset':'b) Basic-5 Size',
            'color':{'rgba':color or [196,110,67,255],'profile':'sRGB-elle-V2-srgbtrc.icc'},
            'size':size,'opacity':1,'erase':erase,
            'geometry':geometry or {'kind':'polyline','points':[[10,64],[110,64]]},
            'pressure_profile':{'interpolation':'linear','samples':[{'at':0,'value':1},{'at':1,'value':1}]},
            'timing':{'duration_ms':500},'expected_bounds':{'x':0,'y':0,'width':128,'height':128},
            'limits':{'max_points':128,'spacing':4,'tolerance':.3}}
