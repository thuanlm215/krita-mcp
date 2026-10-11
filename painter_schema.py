"""JSON schemas for painter clients; stdlib-only, no Krita imports."""

def obj(properties,optional=()):
    return {'type':'object','properties':properties,'required':[k for k in properties if k not in optional],
            'additionalProperties':False}


def number(lo,hi):return {'type':'number','minimum':lo,'maximum':hi}


POINT={'type':'array','minItems':2,'maxItems':2,'items':number(-100000,100000)}
PROFILE={'type':'string','enum':['sRGB-elle-V2-srgbtrc.icc']}
TARGET=obj({'document':{'type':'string','pattern':'^session:'},'layer':{'type':'string','pattern':'^uuid:'},
    'scope':{'oneOf':[obj({'kind':{'enum':['static']}}),obj({'kind':{'enum':['frame']},
        'time':{'type':'integer','minimum':0,'maximum':99999},
        'exposure_policy':{'enum':['existing_keyframe','one_sample']}})]}})

STROKE=obj({'version':{'type':'integer','enum':[1]},'stroke_id':{'type':'string','minLength':1,'maxLength':128},
    'target':TARGET,'preset':{'type':'string','minLength':1,'description':'Exact installed resource name; calibrated b) Basic-5 Size and g) Dry Bristles.'},
    'color':obj({'rgba':{'type':'array','minItems':4,'maxItems':4,'items':{'type':'integer','minimum':0,'maximum':255}},'profile':PROFILE}),
    'size':number(.1,2048),'opacity':number(0,1),'erase':{'type':'boolean'},
    'geometry':{'oneOf':[obj({'kind':{'enum':['polyline']},'points':{'type':'array','minItems':2,'maxItems':128,'items':POINT}}),
        obj({'kind':{'enum':['cubic']},'segments':{'type':'array','minItems':1,'maxItems':16,
             'items':{'type':'array','minItems':4,'maxItems':4,'items':POINT}}})]},
    'pressure_profile':obj({'interpolation':{'enum':['linear']},'samples':{'type':'array','minItems':2,'maxItems':32,
         'description':'Strictly increasing normalized distance; first at 0, last at 1.',
         'items':obj({'at':number(0,1),'value':number(0,1)})}}),
    'timing':obj({'duration_ms':dict(number(.1,100000),description='Constant speed along flattened arc length.')}),
    'expected_bounds':obj({'x':number(-100000,100000),'y':number(-100000,100000),
                          'width':number(0,100000),'height':number(0,100000)}),
    'limits':obj({'max_points':{'type':'integer','minimum':2,'maximum':128},'spacing':number(.1,2048),'tolerance':number(.001,2)}),
    'tilt':obj({'x':{'type':'number','enum':[0]},'y':{'type':'number','enum':[0]}})},optional=('tilt',))

FRAMES={'type':'array','minItems':1,'maxItems':64,'description':'Increasing integer times, beginning at 0; PNG dimensions/padding must agree.',
        'items':obj({'time':{'type':'integer','minimum':0,'maximum':99998},'path':{'type':'string','minLength':1}})}
