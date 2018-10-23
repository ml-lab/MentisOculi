from PIL import Image
import numpy as np
import torch as tr
from helpers import *
import os
import time
import math
import itertools


from functools import reduce


def no_repeats(l):
    t = l.sort()[0]
    return not (t[1:] == t[:-1]).any()

def save_img(args, color, nm):
    file_nm = os.path.join(args.SAVE_DIR,nm)
    print("\tsaving:", file_nm)
    color = color * 10
    rgb = [Image.fromarray(np.array(c.clamp(0, 1).reshape((args.h, args.w)).float() * 255), "F").resize((args.WIDTH, args.HEIGHT), Image.ANTIALIAS).convert("L") for c in color.components()]
    Image.merge("RGB", rgb).save(file_nm)


def random_spherical(u, v):
    theta  = u * 2 * math.pi
    phi = v * math.pi 

    # Switch to cartesian coordinates
    sphi = phi.sin()
    x = theta.cos() * sphi
    y = theta.sin() * sphi

    return vec3(x, y, phi.cos())

class Sphere:
    def __init__(self, center, r, diffuse, mirror = None):
        self.c = center
        self.r = r
        self.diffuse = diffuse
        self.mirror = mirror

    def intersect(self, args, O, D):
        b = 2 * D.dot(O - self.c)
        c = abs(self.c) + abs(O) - 2 * self.c.dot(O) - (self.r * self.r)
        disc = (b ** 2) - (4 * c)
        sq = tr.sqrt(tr.relu(disc))
        h0 = (-b - sq) / 2
        h1 = (-b + sq) / 2
        h = tr.where((h0 > 0) & (h0 < h1), h0, h1)
        pred = (disc > 0) & (h > args.NEAREST)
        return tr.where(pred, h, ones_like(h) * args.FARAWAY)

    def diffusecolor(self, M):
        return self.diffuse

    def sampleDiffuse(self, args, getRand, M, N, newO, bounce):
        rayDiff = random_spherical(getRand(), getRand())
        should_flip = N.dot(rayDiff).lt(0).double()
        rayDiff = rayDiff * (1 - 2 * should_flip)

        return raytrace(args, getRand, newO, rayDiff , bounce + 2) * rayDiff.dot(N) * self.diffusecolor(M) * 2 

    def sampleMirror(self, args, getRand, D, N, newO, bounce):
        rayRefl = (D - N * 2 * D.dot(N)).norm()  # reflection            
        return raytrace(args, getRand, newO, rayRefl, bounce + 1) * self.mirror * rayRefl.dot(N)        


    def light(self, args, getRand, O, D, d, bounce):
        # D is direction
        # O is previous origin
        M = (O + D * d)                         # new intersection point
        N = (M - self.c) / self.r        # normal

        toO = (O - M).norm()                    # direction to ray origin
        newO = M + N * args.NUDGE               # M nudged to avoid itself

        if self.mirror is not None:
            diffcol = self.diffusecolor(M)
            refl_prob = self.mirror / (self.mirror + diffcol.luminance()) if isinstance(self.mirror, numbers.Number) else self.mirror.luminance()
            reflect = tri(getRand()) <= refl_prob
            diffuse = 1 - reflect
            
            colorDiff = self.sampleDiffuse(args, getNewRand(getRand, diffuse, 0), M.extract(diffuse), N.extract(diffuse), newO.extract(diffuse), bounce) * (1 / (1 - refl_prob)) if diffuse.any() else rgb(0,0,0)

            colorRefl = self.sampleMirror(args, getNewRand(getRand, reflect, 1), D.extract(reflect), N.extract(reflect), newO.extract(reflect), bounce) * (1 / refl_prob) if reflect.any() else rgb(0,0,0)

            color = colorDiff.place(diffuse) + colorRefl.place(reflect)
        else:
            color = self.sampleDiffuse(args, getRand, M, N, newO, bounce)
        return color


class CheckeredSphere(Sphere):
    def diffusecolor(self, M):
        checker = ((M.x * 2).int() % 2) == ((M.z * 2).int() % 2)
        return self.diffuse * checker.double() + rgb(0.8, 0.6, 0.6) * (1 - checker.double())

class Light(Sphere):
    def light(self, *args, **kargs):
        return self.diffuse
  

def raytrace(args, getRand, O, D, bounce = 0):
    # O is the ray origin, D is the normalized ray direction
    # scene is a list of Sphere objects (see below)
    # bounce is the number of the bounce, starting at zero for camera rays
    color = rgb(0, 0, 0)
    if bounce > args.MAX_BOUNCE:
        return color

    distances = [dtype(s.intersect(args, O, D)) for s in args.scene]
    nearest, nearest_idx = tr.min(tr.stack(distances), dim=0)

    for (s, i) in zip(args.scene, range(len(args.scene))):
        hit = (nearest < args.FARAWAY) & (nearest_idx == i) & (nearest > args.NUDGE) # d == nearest is hacky af
        probStop = args.STOP_PROB if bounce >= 1 else 0
        rd = tri(getRand())
        rgp = (rd >= probStop)

        hit = hit & rgp

        if hit.any():
            Oc = O.extract(hit)
            dc = extract(hit, nearest)
            Dc = D.extract(hit)
            cc = s.light(args, getNewRand(getRand, hit, i), Oc, Dc, dc, bounce)
            color += cc.place(hit) / (1 - probStop)

    return color


def getNewRand(getRand, mask, curr_idx):
    if mask.all():
        return getRand
    mshape = [int(mask.sum(dtype=tr.long))]
    def newRand(arg = None):
        if arg is None:
            arg = (mshape, mask, [curr_idx])
            #pdbAssert(product(mshape) == int(mask.sum(dtype=tr.long)))
        else:
            (sN, hitN, sub_idx) = arg
            maskN = place(mask, hitN)
            arg = (sN, maskN, [curr_idx] + sub_idx)
            #pdbAssert(product(sN) == int(maskN.sum(dtype=tr.long)))
        return getRand(arg)
    return newRand

def getMCRand(top_shape):
    def getRand(arg = None):
        if arg is None:
            mask = lones(top_shape, dtype=tr.uint8)
            maskShape = top_shape
            idx = []
        else:
            maskShape,mask, idx = arg
            #pdbAssert(product(maskShape) == int(mask.sum(dtype=tr.long)))
        return rand(size = maskShape)
    return getRand

def getPermuteRand(top_shape, mcmc_best):
    mcmc_generator = {}
    num_calls = {}

    for k,v in mcmc_best.items():  # save old random values for when new things get mixed in
        mcmc_generator[k] = v
    def getRand(arg = None):
            if arg is None:
                mask = lones(top_shape, dtype=tr.uint8)
                maskShape = top_shape
                idx = []
            else:
                maskShape, mask, idx = arg
                #pdbAssert(product(maskShape) == int(mask.sum(dtype=tr.long)))
            tidx = tuple(idx)    
            
            if tidx not in num_calls:
                num_calls[tidx] = 0
            else:
                num_calls[tidx] += 1
            tidx = tuple(idx + [num_calls[tidx]])

            if tidx not in mcmc_best:
                r = rand(size = maskShape)
                #pdbAssert(product(r.shape) == product(maskShape))
            else: 
                # could be done way quicker in handwritten cuda.
                # sadly, pseudorandoms are slow enough that we want to do as few of them as possible.
                
                bestIndxs, bestRand = mcmc_best[tidx]

                newRands = zeros(top_shape) # if these are different sizes then something went very significantly wrong
                newRands[mask] = rand(size = maskShape)

                newRands[cudify(bestIndxs)] = bestRand + randn(bestRand.shape) * 0.003
                
                r = newRands[mask].contiguous()
                #pdbAssert(product(r.shape) == product(maskShape))
                #r.sub_(r.floor())
                #

            ids = mask.nonzero().squeeze(dim=1)
            #pdbAssert(no_repeats(ids))
            mcmc_generator[tidx] = (ids.cpu(),r)
            return r
    
    return getRand, mcmc_generator

def mixSamples(top_shape, mix, sa, sb):
    res = {}
            
    for k in set().union(sa.keys(), sb.keys()):
        if k not in sa.keys():
            res[k] = sb[k]
        elif k not in sb.keys():
            res[k] = sa[k]
        else:
            aI, aR = sa[k]
            
            bI, bR = sb[k]

            aM = lzeros(top_shape, dtype=tr.uint8)
            bM = lzeros(top_shape, dtype=tr.uint8)
            
            aM[aI] = 1
            bM[bI] = 1

            aRes = zeros(top_shape)
            bRes = zeros(top_shape)

            aRes[aM] = aR
            bRes[bM] = bR
            
            abM = aM | bM

            # be wary of what happens when mixing something in which was not there before!
            abMn = abM.nonzero().squeeze(dim=1)
            #pdbAssert(no_repeats(abMn))
            res[k] = (abMn.cpu(), (aRes * mix + bRes * (1 - mix))[abM])
    return res

def multiSamp(args, samp_shape, samp_cast, num_mc_samples):
    total_time = 0
    estimate = vec3u(0,samp_shape)
    samps_per_pass = product(samp_shape)
    for i in range(1,num_mc_samples + 1):
        tPass = time.time()

        mcRand = getMCRand(samp_shape)
        new_estimate = raytrace(args, mcRand, args.eye, (samp_cast - args.eye).norm(), bounce = 0) 
        estimate = (new_estimate / float(num_mc_samples))  + estimate

        tCurr = time.time()
        pass_time = tCurr - tPass
        total_time += pass_time

        print("\nMCPass:", i)
        print("\tElapsed Time:", total_time)
        print("\tPass Time:", pass_time)
        print("\tAvg Pass Time:",  total_time / i)

        print("\tTotal Samples:", samps_per_pass * i)
        print("\tSamples Per Pixel:", args.OVERSAMPLE * i)

        print("\tsamp/sec:", samps_per_pass / pass_time )
        print("\tAvg samp/sec:",  samps_per_pass * i / total_time, "\n")

    return estimate

def one_or_div(a,b, o = 1):
    if isinstance(b, numbers.Number):
        return a / b if b > 0 else 1
    gtz = b > 0
    return tr.where(gtz, a / tr.where(gtz, b, ones(b.shape) * o) , ones(b.shape) * o)

def addS(args, img, s, p):
    im_locs = s * vec3(args.w, args.h, 0)
    im_locs = [im_locs.y.long(), im_locs.x.long()]
    
    img.x.reshape(args.h, args.w)[im_locs] += p.x
    img.y.reshape(args.h, args.w)[im_locs] += p.y
    img.z.reshape(args.h, args.w)[im_locs] += p.z

def wrap(r):
    return r - r.floor()

def tri(r):
    return 1 - (1 - r.fmod(2)).abs()


def pathtrace(args, S):

    samp_shape = [args.WIDTH * args.SUBSAMPLE * args.HEIGHT * args.SUBSAMPLE]
    img_shape  = [args.w * args.h]

    samps_per_pass = product(samp_shape)

    histogram = vec3u(0, img_shape)

    total_time = 0

    x_sz = (S[2] - S[0])
    y_sz = (S[3] - S[1])

    m = 0
    k = 0
    mc_histogram = vec3u(0, img_shape)
    for i in itertools.count(1,1):
        restart = i % args.restart_freq == 1     
        if restart:
            best_samp = vec3u(0, samp_shape)
            best_samp_params = {}
        elif i % args.mut_restart_freq == 1:
            best_samp = vec3u(0, samp_shape)
            best_samp_coords = original_samp_coords
            best_samp_params = original_samp_params

        getRand, new_samp_params = getPermuteRand(samp_shape, best_samp_params)
        samp_coords = vec3(tri(getRand()), tri(getRand()), 0)

        samp_cast = vec3(S[0], S[1], 0) + samp_coords * vec3(x_sz, y_sz, 0)

        if restart:
            k += 1
            original_samp_coords = samp_coords
            original_samp_params = new_samp_params

            best_samp_coords = samp_coords
            best_samp_params = new_samp_params

            estimate = multiSamp(args, samp_shape, samp_cast, args.num_mc_samples)

            addS(args, mc_histogram, best_samp_coords, estimate)
            save_img(args, mc_histogram / k, "estimate"+str(k)+".png")
            continue

        m += 1

        tPass = time.time()

        new_samp = raytrace(args, getRand, args.eye, (samp_cast - args.eye).norm(), bounce = 0) 

        accept_var = rand(samp_shape)
        accept_prob = one_or_div(new_samp.luminance(), best_samp.luminance())
        accept_prob.clamp_(0,1)

        addS(args, histogram, best_samp_coords, (best_samp * estimate.luminance()).div_or(best_samp.luminance(), estimate) * (1 - accept_prob) )
        addS(args, histogram, samp_coords, (new_samp * estimate.luminance()).div_or(new_samp.luminance(), estimate) * accept_prob)

        should_accept = (accept_var <= accept_prob).double()
        best_samp_params = mixSamples(samp_shape, should_accept, new_samp_params, best_samp_params)
        best_samp = new_samp * should_accept + best_samp * (1 - should_accept)
        best_samp_coords = samp_coords * should_accept + best_samp_coords * (1 - should_accept)


        tCurr = time.time()
        pass_time = tCurr - tPass
        total_time += pass_time

        print("\n\nPass:", i)
        print("\tElapsed Time:", total_time)
        print("\tPass Time:", pass_time)
        print("\tAvg Pass Time:",  total_time / i)

        print("\n\tTotal Samples:", samps_per_pass * i)
        print("\tSamples Per Pixel:", args.OVERSAMPLE * i)

        print("\n\tsamp/sec:", samps_per_pass / pass_time )
        print("\tAvg samp/sec:",  samps_per_pass * i / total_time, "\n")

        save_img(args, histogram / m, "img"+str(i)+".png")
        save_img(args, histogram / m, "img.png")


def render(args):

    if not os.path.exists(args.SAVE_DIR):
        os.makedirs(args.SAVE_DIR)

    args.w = args.WIDTH * args.OVERSAMPLE
    args.h = args.HEIGHT * args.OVERSAMPLE

    r = float(args.WIDTH) / args.HEIGHT
    S = (-1., 1. / r + .25, 1., -1. / r + .25)
    pathtrace(args, S)


class StaticArgs:
    SAVE_DIR="out_small"
    OVERSAMPLE = 2

    SUBSAMPLE = 2

    WIDTH = 400
    HEIGHT = 300

    scene = [
        Light(vec3(5, 2, 1.2), 2.0, rgb(1, 1, 1)),
        Sphere(vec3(0, 205, 1), 197, rgb(0.99, 0.96, 0.99)),
        Sphere(vec3(.3, .1, 1.3), .6, rgb(0.1, 0.1, 0), rgb(0.9, 0.95, 1)),
        Sphere(vec3(-.4, .2, 0.8), .4, rgb(1, .8, .9).rgbNorm() * 3 * 0.4, 0.7),
        CheckeredSphere(vec3(0,-99999.5, 0), 99999, rgb(.96, .99, .99)),
    ]

    eye = vec3(0., 0.35, -1.)     # Eye position
    FARAWAY = 1.0e36            # an implausibly huge distance
    MAX_BOUNCE = 12
    NUDGE = 0.0000001
    STOP_PROB = 0.75

    NEAREST = 0.000000001
    restart_freq = 30
    mut_restart_freq = 10
    num_mc_samples = 20

render(StaticArgs)
