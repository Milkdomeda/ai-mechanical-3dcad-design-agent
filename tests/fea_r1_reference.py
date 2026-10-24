"""Independent reference: integrate analytic polynomials over a curved graph.

For x=r, y=s, z=k*r*r/2, J=sqrt(1+k*k*r*r). The s integral
of each shape function is a literal polynomial below. Adaptive Simpson in
one dimension is independent of the native 2-D Gaussian/Jacobian code.
"""
import math


def integral(function, tolerance=2e-15):
    def segment(a,b,fa,fm,fb,whole,budget,depth):
        m=(a+b)/2; l=(a+m)/2; r=(m+b)/2
        fl,fr=function(l),function(r)
        left=(m-a)*(fa+4*fl+fm)/6
        right=(b-m)*(fm+4*fr+fb)/6
        delta=left+right-whole
        if abs(delta)<=15*budget:return left+right+delta/15
        if not depth:raise AssertionError('reference integration did not converge')
        return segment(a,m,fa,fl,fm,left,budget/2,depth-1)+segment(m,b,fm,fr,fb,right,budget/2,depth-1)
    a,m,b=0.,.5,1.;fa,fm,fb=map(function,(a,m,b))
    return segment(a,b,fa,fm,fb,(fa+4*fm+fb)/6,tolerance,25)


def graph_points(k=.5):
    return ((0.,0.,0.),(1.,0.,k/2),(0.,1.,0.),(.5,0.,k/8),(.5,.5,k/8),(0.,.5,0.))


def graph_reference(k=.5):
    j=lambda r:math.sqrt(1+k*k*r*r)
    polynomials=(lambda r:2*(1-r)**3/3-(1-r)**2/2,
                 lambda r:(2*r*r-r)*(1-r),
                 lambda r:2*(1-r)**3/3-(1-r)**2/2,
                 lambda r:2*r*(1-r)**2,
                 lambda r:2*r*(1-r)**2,
                 lambda r:2*(1-r)**3/3)
    return {'weights':tuple(integral(lambda r,p=p:p(r)*j(r)) for p in polynomials),
            'area':integral(lambda r:(1-r)*j(r)),
            'x':integral(lambda r:r*(1-r)*j(r)),
            'y':integral(lambda r:(1-r)**2*j(r)/2),
            'x2':integral(lambda r:r*r*(1-r)*j(r))}
