"""Pixel statistics and geometric diagnostics on copied BGRA/U8 buffers."""
from collections import Counter, deque
import math


def linear(v):
    v=v/255
    return v/12.92 if v<=.04045 else ((v+.055)/1.055)**2.4


def srgb(v):
    v=max(0,min(1,v))
    return round(255*(12.92*v if v<=.0031308 else 1.055*v**(1/2.4)-.055))


def statistics(raw, palette_size=8):
    pixels=[(raw[i+2],raw[i+1],raw[i],raw[i+3]) for i in range(0,len(raw),4)]
    visible=[p for p in pixels if p[3]]
    weight=sum(p[3] for p in visible)
    bins=Counter();counts=Counter()
    for r,g,b,a in visible:
        key=(r//16,g//16,b//16);bins[key]+=a;counts[key]+=1
    return {'pixel_count':len(pixels),'visible_count':len(visible),'empty':not visible,
            'alpha_policy':'ignore alpha zero; weight RGB by alpha/255',
            'linear_light_mean_rgb':None if not visible else [srgb(sum(linear(p[c])*p[3] for p in visible)/weight) for c in range(3)],
            'palette_quantization':'4 bits/channel; centers 16*k+8 (clamped); descending alpha weight, then RGB bin',
            'palette':[{'rgb':[min(255,v*16+8) for v in k],'count':counts[k],
                        'alpha_weight':bins[k]/255} for k in sorted(bins,key=lambda k:(-bins[k],k))[:palette_size]]}


def boundary(mask,w,h):
    return {i for i,v in enumerate(mask) if v and
            (i%w==0 or i%w==w-1 or i<w or i>=w*(h-1) or
             not mask[i-1] or not mask[i+1] or not mask[i-w] or not mask[i+w])}


def distances(seeds,w,h):
    out=[w+h]*(w*h);q=deque(seeds)
    for i in seeds:out[i]=0
    while q:
        i=q.popleft();x,y=i%w,i//w
        for j in ([i-1] if x else [])+([i+1] if x<w-1 else [])+([i-w] if y else [])+([i+w] if y<h-1 else []):
            if out[j]>out[i]+1:out[j]=out[i]+1;q.append(j)
    return out


def measurements(before,after,w,h,allowed=None,target=None,threshold=128):
    if len(before)!=w*h*4 or len(after)!=len(before):raise ValueError('Invalid comparison buffers')
    if allowed is not None and len(allowed)!=w*h or target is not None and len(target)!=w*h:
        raise ValueError('Mask size mismatch')
    changed=[i for i in range(w*h) if before[i*4:i*4+4]!=after[i*4:i*4+4]]
    outside=[i for i in changed if allowed is not None and allowed[i]==0]
    bounds=None
    if changed:
        xs=[i%w for i in changed];ys=[i//w for i in changed]
        bounds={'x':min(xs),'y':min(ys),'width':max(xs)-min(xs)+1,'height':max(ys)-min(ys)+1}
    result={'changed_pixels':len(changed),'changed_bounds':bounds,'outside_allowed_pixels':len(outside),
            'alpha_threshold':threshold,'mask_rule':'allowed alpha 0 forbids every stored-byte change; target alpha >= threshold is foreground'}
    if target is not None:
        a=[after[i*4+3]>=threshold for i in range(w*h)];t=[v>=threshold for v in target]
        overlap=sum(x and y for x,y in zip(a,t));union=sum(x or y for x,y in zip(a,t));total=sum(t)
        ba,bt=boundary(a,w,h),boundary(t,w,h)
        error=0 if not ba and not bt else None
        if ba and bt:
            da,dt=distances(ba,w,h),distances(bt,w,h)
            error=(sum(dt[i] for i in ba)+sum(da[i] for i in bt))/(len(ba)+len(bt))
        result.update(target_iou=overlap/union if union else 1,
                      target_coverage=overlap/total if total else (1 if not any(a) else 0),
                      symmetric_boundary_distance=error,boundary_distance_unit='document pixel Manhattan distance; undefined for one empty boundary')
    diff=bytearray(w*h*4);violations=bytearray(w*h*4)
    for i in changed:diff[i*4:i*4+4]=bytes((200,40,255,255))
    for i in outside:violations[i*4:i*4+4]=bytes((0,0,255,255))
    return result,bytes(diff),bytes(violations)
