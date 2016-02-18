import os
import imp
import time
import logging
import numpy as np
import scipy.sparse
import scipy.sparse.linalg
from bmi.api import IBmi

# package modules
import io, bed, wind, threshold, transport, hydro, netcdf, constants
from utils import *


# initialize logger
logger = logging.getLogger(__name__)


class AeoLiS(IBmi):
    '''AeoLiS model class

    AeoLiS is a process-based model for simulating supply-limited
    aeolian sediment transport. This model class is compatible with
    the Basic Model Interface (BMI) and provides basic model
    operations, like initialization, time stepping, finalization and
    data exchange. For higher level operations, like a progress
    indicator and netCDF4 output is refered to the AeoLiS model
    runner class, see :class:`~aeolis.model.AeoLiSRunner()`.

    Examples
    --------
    >>> with AeoLiS(configfile='aeolis.txt') as model:
    >>>     while model.get_current_time() <= model.get_end_time():
    >>>         model.update()

    >>> model = AeoLiS(configfile='aeolis.txt')
    >>> model.initialize()
    >>> zb = model.get_var('zb')
    >>> model.set_var('zb', zb + 1)
    >>> for i in range(10):
    >>>     model.update(60.) # step 60 seconds forward
    >>> model.finalize()

    '''

    t = 0.
    dt = 0.
    configfile = ''

    l = {} # previous spatial grids
    s = {} # spatial grids
    p = {} # parameters
    c = {} # counters
    
    
    def __init__(self, configfile):
        '''Initialize class

        Parameters
        ----------
        configfile : str
            Model configuration file. See :func:`~aeolis.io.read_configfile()`.

        '''
        
        self.configfile = configfile


    def __enter__(self):
        self.initialize()
        return self


    def __exit__(self, *args):
        self.finalize()

    
    def initialize(self):
        '''Initialize model

        Read model configuration file and initialize parameters and
        spatial grids dictionary and load bathymetry and bed
        composition.

        '''

        # read configuration file
        self.p = io.read_configfile(self.configfile)
        io.check_configuration(self.p)

        # initialize time
        self.t = self.p['tstart']

        # get model dimensions
        nx = self.p['nx']
        ny = self.p['ny']
        nl = self.p['nlayers']
        nf = self.p['nfractions']

        # initialize spatial grids
        for var, dims in self.dimensions().iteritems():
            self.s[var] = np.zeros(self._dims2shape(dims))
            self.l[var] = self.s[var].copy()

        # initialize bed composition
        self.s = bed.initialize(self.s, self.p)

        
    def update(self, dt=-1):
        '''Time stepping function

        Takes a single step in time. Interpolates wind and
        hydrodynamic time series to the current time, updates the soil
        moisture, mixes the bed due to wave action, computes wind
        velocity threshold and the equilibrium sediment transport
        concentration. Subsequently runs one of the available
        numerical schemes to compute the instantaneous sediment
        concentration and pickup for the next time step and updates
        the bed accordingly.

        For explicit schemes the time step is maximized by the
        Courant-Friedrichs-Lewy (CFL) condition. See
        :func:`~aeolis.model.AeoLiS.set_timestep()`.

        Parameters
        ----------
        dt : float, optional
            Time step in seconds. The time step specified in the model
            configuration file is used in case dt is smaller than
            zero. For explicit numerical schemes the time step is
            maximized by the CFL confition.

        '''

        self.p['_time'] = self.t

        # store previous state
        self.l = self.s.copy()
        
        # interpolate wind time series
        self.s = wind.interpolate(self.s, self.p, self.t)
        
        # determine optimal time step
        if not self.set_timestep(dt):
            return

        # interpolate hydrodynamic time series
        self.s = hydro.interpolate(self.s, self.p, self.t)
        self.s = hydro.update(self.s, self.p, self.dt)

        # mix top layer
        self.s = bed.mixtoplayer(self.s, self.p)

        # compute threshold
        self.s = threshold.compute(self.s, self.p)

        # compute equilibrium transport
        self.s = transport.equilibrium(self.s, self.p)

        # compute instantaneous transport
        if self.p['scheme'] == 'euler_forward':
            self.s.update(self.euler_forward())
        elif self.p['scheme'] == 'euler_backward':
            self.s.update(self.euler_backward())
        elif self.p['scheme'] == 'crank_nicolson':
            self.s.update(self.crank_nicolson())
        else:
            raise ValueError('Unknown scheme [%s]' % self.p['scheme'])

        # update bed
        self.s = bed.update(self.s, self.p)

        # increment time
        self.t += self.dt
        self._count('time')


    def finalize(self):
        '''Finalize model'''
        
        pass
    
        
    def get_current_time(self):
        '''
        Returns
        -------
        float
            Current simulation time

        '''
        
        return self.t
    
        
    def get_end_time(self):
        '''
        Returns
        -------
        float
            Final simulation time

        '''

        return self.p['tstop']
    
        
    def get_start_time(self):
        '''
        Returns
        -------
        float
            Initial simulation time

        '''

        return self.p['tstart']
    
        
    def get_var(self, var):
        '''Returns spatial grid or model configuration parameter

        If the given variable name matches with a spatial grid, the
        spatial grid is returned. If not, the given variable name is
        matched with a model configuration parameter. If a match is
        found, the parameter value is returned. Otherwise, nothing is
        returned.

        Parameters
        ----------
        var : str
            Name of spatial grid or model configuration parameter

        Returns
        -------
        np.ndarray or int, float, str or list
            Spatial grid or model configuration parameter

        Examples
        --------
        >>> # returns bathymetry grid
        ... model.get_var('zb')

        >>> # returns simulation duration
        ... model.get_var('tstop')

        See Also
        --------
        :func:`~aeolis.model.AeoLiS.set_var`

        '''

        if self.s.has_key(var):
            return self.s[var]
        elif self.p.has_key(var):
            return self.p[var]
        else:
            return None
    
        
    def get_var_count(self):
        '''
        Returns
        -------
        int
            Number of spatial grids

        '''
        
        return len(self.s)
    
        
    def get_var_name(self, i):
        '''Returns name of spatial grid by index (in alphabetical order)

        Parameters
        ----------
        i : int
            Index of spatial grid

        Returns
        -------
        str or -1
            Name of spatial grid or -1 in case index exceeds the number of grids

        '''
        
        if len(self.s) > i:
            return sorted(self.s.keys())[i]
        else:
            return -1
    
        
    def get_var_rank(self, var):
        '''Returns rank of spatial grid

        Parameters
        ----------
        var : str
            Name of spatial grid

        Returns
        -------
        int
            Rank of spatial grid or -1 if not found

        '''
        
        if self.s.has_key(var):
            return len(self.s[var].shape)
        else:
            return -1
    
        
    def get_var_shape(self, var):
        '''Returns shape of spatial grid

        Parameters
        ----------
        var : str
            Name of spatial grid

        Returns
        -------
        tuple or int
            Dimensions of spatial grid or -1 if not found

        '''
        
        if self.s.has_key(var):
            return self.s[var].shape
        else:
            return -1
    
        
    def get_var_type(self, var):
        '''Returns variable type of spatial grid

        Parameters
        ----------
        var : str
            Name of spatial grid

        Returns
        -------
        str or int
            Variable type of spatial grid or -1 if not found

        '''

        if self.s.has_key(var):
            return 'double'
        else:
            return -1
    
        
    def inq_compound(self):
        raise NotImplementedError('Method not yet implemented [inq_compound]')
    
        
    def inq_compound_field(self):
        raise NotImplementedError('Method not yet implemented [inq_compound_field]')
    
        
    def set_var(self, var, val):
        '''Sets spatial grid or model configuration parameter

        If the given variable name matches with a spatial grid, the
        spatial grid is set. If not, the given variable name is
        matched with a model configuration parameter. If a match is
        found, the parameter value is set. Otherwise, nothing is set.

        Parameters
        ----------
        var : str
            Name of spatial grid or model configuration parameter
        val : np.ndarray or int, float, str or list
            Spatial grid or model configuration parameter

        Examples
        --------
        >>> # set bathymetry grid
        ... model.set_var('zb', np.array([[0.,0., ... ,0.]]))

        >>> # set simulation duration
        ... model.set_var('tstop', 3600.)

        See Also
        --------
        :func:`~aeolis.model.AeoLiS.get_var`

        '''
        
        if self.s.has_key(var):
            self.s[var] = val
        elif self.p.has_key(var):
            self.p[var] = val
    
        
    def set_var_index(self, i, val):
        '''Set spatial grid by index (in alphabetical order)

        Parameters
        ----------
        i : int
            Index of spatial grid
        val : np.ndarray
            Spatial grid

        '''
        
        var = self.get_var_name(i)
        self.set_var(var, val)
    
        
    def set_var_slice(self):
        raise NotImplementedError('Method not yet implemented [set_var_slice]')


    def set_timestep(self, dt=-1.):
        '''Determine optimal time step

        If no time step is given the optimal time step is
        determined. For explicit numerical schemes the time step is
        based in the Courant-Frierichs-Lewy (CFL) condition. For
        implicit numerical schemes the time step specified in the
        model configuration file is used. Alternatively, a preferred
        time step is given that is maximized by the CFL condition in
        case of an explicit numerical scheme.

        Returns True except when:

        1. No time step could be determined, for example when there is
        no wind and the numerical scheme is explicit. In this case the
        time step is set arbitrarily to one second.

        2. Or when the time step is smaller than -1. In this case the
        time is updated with the absolute value of the time step, but
        no model execution is performed. This funcionality can be used
        to skip fast-forward in time.

        Parameters
        ----------
        df : float, optional
            Preferred time step

        Returns
        -------
        bool
            False if determination of time step was unsuccessful, True otherwise

        '''

        if dt > 0.:
            self.dt = dt
        elif dt < -1:
            self.dt = dt
            self.t += np.abs(dt)
            return False
        else:
            self.dt = self.p['dt']

        if self.p['scheme'] == 'euler_forward':
            if self.p['CFL'] > 0.:
                dtref = np.max(np.abs(self.s['uws']) / self.s['ds']) + \
                        np.max(np.abs(self.s['uwn']) / self.s['dn'])
                if dtref > 0.:
                    self.dt = np.minimum(self.dt, self.p['CFL'] / dtref)
                else:
                    self.dt = np.minimum(self.dt, 1.)
                    return False

        return True

                
    def euler_forward(self):
        '''Convenience function for explicit solver based on Euler forward scheme

        See Also
        --------
        :func:`~aeolis.model.AeoLiS.solve`

        '''
        
        return self.solve(alpha=0., beta=1.)


    def euler_backward(self):
        '''Convenience function for implicit solver based on Euler backward scheme

        See Also
        --------
        :func:`~aeolis.model.AeoLiS.solve`

        '''
        
        return self.solve(alpha=1., beta=1.)

    
    def crank_nicolson(self):
        '''Convenience function for implicit solver based on Crank-Nicolson scheme

        See Also
        --------
        :func:`~aeolis.model.AeoLiS.solve`

        '''

        return self.solve(alpha=.5, beta=1.)

    
    def solve(self, alpha=.5, beta=1.):
        '''Implements the explicit Euler forward, implicit Euler backward and semi-implicit Crank-Nicolson numerical schemes

        Determines weights of sediment fractions, sediment pickup and
        instantaneous sediment concentration. Returns a partial
        spatial grid dictionary that can be used to update the global
        spatial grid dictionary.

        The instantaneous sediment concentration :math:`\widehat{C}_t`
        is solved by solving a linear system of equations based on the
        advection equation:

        .. math::

            \\frac{\\partial \\widehat{C}_t}{\\partial t} 
                + u_w \\frac{\\partial \\widehat{C}_t}{\\partial x}
                = \\frac{w \cdot \\widehat{C}_u - \\widehat{C}_t}{T}

        The corresponding solution is exact, but may violate the
        availability of sediment in the bed:

        .. math::

            \\frac{w \cdot \\widehat{C}_u - \\widehat{C}_t}{T} \\le \\frac{\\partial S_e}{\\partial t}

        If this is the case, the weights :math:`w` for the sediment
        fractions for which a deficit exists are lowered to match
        supply. The weights for other fractions are increased to
        ensure that the sum of all weights remains one (see
        :func:`~aeolis.transport.renormalize_weights()`). Only in case
        all sediment fractions are in deficit, the total pickup of
        sediment is reduced.

        The linear system of equations that is solved depends on the
        selected numerical scheme. The offshore and onshore boundaries
        are Neumann boundaries. The lateral boundaries have circular
        boundary conditions. Therefore the system that is solved
        reads:

        .. include:: ../docs/linear_system.inc

        The coefficients :math:`\\alpha` and :math:`\\beta` are used
        to respectively select the implicitness of the scheme in time
        and the centralization in space. :math:`\\alpha = 0` results
        in the fully explicit Euler forward scheme, :math:`\\alpha =
        1` results in the fully implicit Euler backward scheme, while
        :math:`\\alpha = 0.5` results in the semi-implicit
        Crank-Nicolson scheme. :math:`\\beta = 1` results in an upwind
        scheme for which the direction is adapted to the local wind
        direction, while :math:`\\beta = 0.5` results in a central
        difference scheme (which is instable!).

        Parameters
        ----------
        alpha : float, optional
            Implicitness coefficient (0.0 for Euler forward, 1.0 for Euler backward or 0.5 for Crank-Nicolson, default=0.5)
        beta : float, optional
            Centralization coefficient (1.0 for upwind or 0.5 for centralized, default=1.0)

        Returns
        -------
        dict
            Partial spatial grid dictionary

        Examples
        --------
        >>> model.s.update(model.solve(alpha=1., beta=1.) # euler backward

        >>> model.s.update(model.solve(alpha=.5, beta=1.) # crank-nicolson

        See Also
        --------
        :func:`~aeolis.model.AeoLiS.euler_forward`
        :func:`~aeolis.model.AeoLiS.euler_backward`
        :func:`~aeolis.model.AeoLiS.crank_nicolson`
        :func:`aeolis.transport.compute_weights`
        :func:`aeolis.transport.renormalize_weights`

        '''

        l = self.l
        s = self.s
        p = self.p

        Ct = s['Ct'].copy()
        pickup = s['pickup'].copy()

        # compute transport weights for all sediment fractions
        w = transport.compute_weights(s, p)

        # define matrix coefficients to solve linear system of equations
        Cs = self.dt * s['dn'] * s['dsdni'] * s['uws']
        Cn = self.dt * s['ds'] * s['dsdni'] * s['uwn']
        Ti = self.dt / p['T']

        beta = abs(beta)
        if beta >= 1.:
            # define upwind direction
            ixs = np.asarray(s['uws'] >= 0., dtype=np.float)
            ixn = np.asarray(s['uwn'] >= 0., dtype=np.float)
            sgs = 2. * ixs - 1.
            sgn = 2. * ixn - 1.
        else:
            # or centralizing weights
            ixs = beta + np.zeros(s['uw'])
            ixn = beta + np.zeros(s['uw'])
            sgs = np.zeros(s['uw'])
            sgn = np.zeros(s['uw'])

        # initialize matrix diagonals
        A0 = np.zeros(s['uw'].shape)
        Apx = np.zeros(s['uw'].shape)
        Ap1 = np.zeros(s['uw'].shape)
        Ap2 = np.zeros(s['uw'].shape)
        Amx = np.zeros(s['uw'].shape)
        Am1 = np.zeros(s['uw'].shape)
        Am2 = np.zeros(s['uw'].shape)

        # populate matrix diagonals
        A0 = 1. + (sgs * Cs + sgn * Cn + Ti) * alpha
        Apx = Cn * alpha * (1. - ixn)
        Ap1 = Cs * alpha * (1. - ixs)
        Amx = -Cn * alpha * ixn
        Am1 = -Cs * alpha * ixs

        # add neumann boundaries
        A0[:,0] = 1.
        Apx[:,0] = 0.
        Ap2[:,0] = s['ds'][:,1] / s['ds'][:,2]
        Ap1[:,0] = -1. - s['ds'][:,1] / s['ds'][:,2]
        Amx[:,0] = 0.
        Am2[:,0] = 0.
        Am1[:,0] = 0.

        A0[:,-1] = 1.
        Apx[:,-1] = 0.
        Ap1[:,-1] = 0.
        Amx[:,-1] = 0.
        Am2[:,-1] = s['ds'][:,-1] / s['ds'][:,-2]
        Am1[:,-1] = -1. - s['ds'][:,-1] / s['ds'][:,-2]

        # construct sparse matrix
        if p['ny'] > 0:
            i = p['nx']+1
            A = scipy.sparse.diags((Apx.flatten()[:i],
                                    Amx.flatten()[i:],
                                    Am2.flatten()[2:],
                                    Am1.flatten()[1:],
                                    A0.flatten(),
                                    Ap1.flatten()[:-1],
                                    Ap2.flatten()[:-2],
                                    Apx.flatten()[i:],
                                    Amx.flatten()[:i]),
                                   (-i*p['ny'],-i,-2,-1,0,1,2,i,i*p['ny']), format='csr')
        else:
            A = scipy.sparse.diags((Am2.flatten()[2:],
                                    Am1.flatten()[1:],
                                    A0.flatten(),
                                    Ap1.flatten()[:-1],
                                    Ap2.flatten()[:-2]),
                                   (-2,-1,0,1,2), format='csr')

        # solve transport for each fraction separately using latest
        # available weights
        nf = p['nfractions']
        for i in range(nf):

            # renormalize weights for all fractions equal or larger
            # than the current one such that the sum of all weights is
            # unity
            w = transport.renormalize_weights(w, i)

            # iteratively find a solution of the linear system that
            # does not violate the availability of sediment in the bed
            for n in range(p['max_iter']):
                self._count('matrixsolve')

                # create the right hand side of the linear system
                y_i = np.zeros(s['uw'].shape)
                y_i[:,1:-1] = l['Ct'][:,1:-1,i] \
                    + alpha * w[:,1:-1,i] * s['Cu'][:,1:-1,i] * Ti \
                    + (1. - alpha) * (
                        l['w'][:,1:-1,i] * l['Cu'][:,1:-1,i] * Ti \
                        - (sgs[:,1:-1] * Cs[:,1:-1] + \
                           sgn[:,1:-1] * Cn[:,1:-1] + Ti) * l['Ct'][:,1:-1,i] \
                        + ixs[:,1:-1] * Cs[:,1:-1] * l['Ct'][:,:-2,i] \
                        - (1. - ixs[:,1:-1]) * Cs[:,1:-1] * l['Ct'][:,2:,i] \
                        + ixn[:,1:-1] * Cn[:,1:-1] * np.roll(l['Ct'][:,1:-1,i],
                                                             1, axis=0) \
                        - (1. - ixn[:,1:-1]) * Cn[:,1:-1] * np.roll(l['Ct'][:,1:-1,i],
                                                                    -1, axis=0) \
                    )

                # solve system with current weights, determine pickup
                # and deficit for current fraction
                Ct_i = scipy.sparse.linalg.spsolve(A, y_i.flatten())
                Cu_i = s['Cu'][:,:,i].flatten()
                mass_i = s['mass'][:,:,0,i].flatten()
                w_i = w[:,:,i].flatten()
                pickup_i = (w_i * Cu_i - Ct_i) / p['T'] * self.dt
                deficit_i = pickup_i - mass_i
                ix = (deficit_i > p['max_error']) \
                     & (w_i * Cu_i > 0.)

                # quit the iteration if there is no deficit, otherwise
                # back-compute the maximum weight allowed to get zero
                # deficit fo r the current fraction and progress to
                # the next iteration step
                if not np.any(ix):
                    logger.debug('Iteration converged [steps: %d, fraction: %d, time: %0.1f]' % (n, i, self.t))
                    pickup_i = np.minimum(pickup_i, mass_i)
                    break
                else:
                    w_i[ix] = (mass_i[ix] * p['T'] / self.dt \
                               + Ct_i[ix]) / Cu_i[ix]
                    w[:,:,i] = w_i.reshape(y_i.shape)

            # throw warning if the maximum number of iterations was
            # reached
            if np.any(ix):
                logger.warn('Iteration not converged [fraction: %d, # cells: %d, time: %0.1f]' % \
                            (i, np.sum(ix), self.t))

            Ct[:,:,i] = Ct_i.reshape(y_i.shape)
            pickup[:,:,i] = pickup_i.reshape(y_i.shape)

        # check if there are any cells where the sum of all weights is
        # smaller than unity. these cells are supply-limited for all
        # fractions. Log these events.
        ix = 1. - np.sum(w, axis=2) > p['max_error']
        if np.any(ix):
            self._count('supplylim')
            logger.warn('Ran out of sediment [# cells: %d, time: %0.1f]' % (np.sum(ix), self.t))
            logger.debug('Minimum sum of weights is %0.4f' % np.sum(w, axis=2)[ix].min())
                            
        return dict(Ct=Ct,
                    pickup=pickup,
                    w=w)


    def get_count(self, name):
        '''Get counter value

        Parameters
        ----------
        name : str
            Name of counter

        '''

        if self.c.has_key(name):
            return self.c[name]
        else:
            return 0

        
    def _count(self, name, n=1):
        '''Increase counter

        Parameters
        ----------
        name : str
            Name of counter
        n : int, optional
            Increment of counter (default: 1)

        '''
        
        if not self.c.has_key(name):
            self.c[name] = 0
        self.c[name] += n


    def _dims2shape(self, dims):
        '''Converts named dimensions to numbered shape

        Supports only dimension names that can be found in the model
        parameters dictionary. The dimensions ``nx`` and ``ny`` are
        increased by one, so they match the size of the spatial grids
        rather than the number of spatial cells in the model.

        Parameters
        ----------
        dims : iterable
            Iterable with strings specifying dimension names

        Returns
        -------
        tuple
            Shape of spatial grid

        '''
        
        shape = []
        for dim in dims:
            shape.append(self.p[dim])
            if dim in ['nx', 'ny']:
                shape[-1] += 1
        return tuple(shape)
    

    @staticmethod
    def dimensions(var=None):
        '''Static method that returns named dimensions of all spatial grids

        Parameters
        ----------
        var : str, optional
            Name of spatial grid

        Returns
        -------
        tuple or dict
            Tuple with named dimensions of requested spatial grid or
            dictionary with all named dimensions of all spatial
            grids. Returns nothing if requested spatial grid is not
            defined.

        '''
        
        dims = {}
        
        dims.update({v:('ny','nx')
                     for v in ['x', 'y', 'zb', 'ds', 'dn', 'dsdn', 'dsdni',
                               'alfa', 'uw', 'uws', 'uwn', 'udir', 'zs', 'Hs']})
        dims.update({v:('ny','nx','nfractions')
                     for v in ['Cu', 'Ct', 'pickup', 'w', 'uth']})
        dims.update({v:('ny','nx','nlayers')
                     for v in ['thlyr', 'moist']})
        dims.update({v:('ny','nx','nlayers','nfractions')
                     for v in ['mass']})

        if var is not None:
            if dims.has_key(var):
                return dims[var]
            else:
                return None
        else:
            return dims
        

class AeoLiSRunner(AeoLiS):
    '''AeoLiS model runner class

    This runner class is a convenience class for the BMI-compatible
    AeoLiS model class (:class:`~aeolis.model.AeoLiS()`). It implements
    a time loop, a progress indicator and netCDF4 output. It also
    provides the definition of a callback function that can be used to
    interact with the AeoLiS model during runtime.

    The command-line function ``aeolis`` is available that uses this
    class to start an AeoLiS model run.

    Examples
    --------
    >>> # run with default settings
    ... AeoLiSRunner().run()

    >>> AeoLiSRunner(configfile='aeolis.txt').run()

    >>> model = AeoLiSRunner(configfile='aeolis.txt')
    >>> model.run(callback=lambda model: model.set_var('zb', zb))

    >>> model.run(callback='bar.py:add_bar')

    See Also
    --------
    aeolis.cmd.aeolis

    '''
    

    t0 = None
    tout = 0.
    tlog = 0.
    plog = -1.

    n = 0 # time step counter
    o = {} # output stats

    clear = False
    changed = False

    
    def __init__(self, configfile='aeolis.txt'):
        '''Initialize class

        Reads model configuration file without parsing all referenced
        files for the progress indicator and netCDF output. If no
        configuration file is given, the default settings are used.
        
        Parameters
        ----------
        configfile : str, optional
            Model configuration file. See :func:`~aeolis.io.read_configfile()`.

        '''

        self.set_configfile(configfile)
        if os.path.exists(self.configfile):
            self.p = io.read_configfile(self.configfile, parse_files=False)
            self.changed = False
        elif self.configfile.upper() == 'DEFAULT':
            self.changed = True
            self.p = constants.DEFAULT_CONFIG

            # add default profile and time series
            self.p.update(dict(nx         = 99,
                               ny         = 0,
                               xgrid_file = np.arange(0.,100.,1.),
                               ygrid_file = np.zeros((1,100)),
                               bed_file   = np.linspace(-5.,5.,100.),
                               wind_file  = np.asarray([[0.,10.,0.],
                                                        [3601.,10.,0.]])))
        else:
            raise IOError('Configuration file not found [%s]' % self.configfile)


    def run(self, callback=None):
        '''Start model time loop

        Changes current working directory to the model directory,
        prints model configuration parameters and progress indicator
        to the screen, writes netCDF4 output and calls a callback
        function upon request.

        Parameters
        ----------
        callback : str or function
            The callback function is called at the start of every
            single time step and takes the AeoLiS model object as
            input. The callback function can be used to interact with
            the model during simulation (e.g. update the bed with new
            measurements). See for syntax
            :func:`~aeolis.model.AeoLiSRunner.parse_callback()`.

        See Also
        --------
        :func:`~aeolis.model.AeoLiSRunner.parse_callback`

        '''

        # http://www.patorjk.com/software/taag/
        # font: Colossal

        print '**********************************************************'
        print '                                                          '
        print '         d8888                   888      d8b  .d8888b.   '
        print '        d88888                   888      Y8P d88P  Y88b  '
        print '       d88P888                   888          Y88b.       '
        print '      d88P 888  .d88b.   .d88b.  888      888  "Y888b.    '
        print '     d88P  888 d8P  Y8b d88""88b 888      888     "Y88b.  '
        print '    d88P   888 88888888 888  888 888      888       "888  '
        print '   d8888888888 Y8b.     Y88..88P 888      888 Y88b  d88P  '
        print '  d88P     888  "Y8888   "Y88P"  88888888 888  "Y8888P"   '
        print '                                                          '

        # set working directory
        fpath, fname = os.path.split(self.configfile)
        if fpath != os.getcwd():
            os.chdir(fpath)
            logger.info('  Changed working directory to: %s\n' % fpath)

        # print settings
        self.print_params()

        # write settings
        self.write_params()

        # parse callback
        callback = self.parse_callback(callback)
        if callback is not None:
            logger.info('  Applying callback function: %s()\n' % callback.__name__)

        # initialize model
        self.initialize()

        # start model loop
        self.t0 = time.time()
        while self.t <= self.p['tstop']:
            if callback is not None:
                callback(self)
            self.update()
            self.output_write()
            self.print_progress()

        # finalize model
        self.finalize()

        self.print_stats()


    def set_configfile(self, configfile):
        '''Set model configuration file name'''

        self.changed = False
        self.configfile = os.path.abspath(configfile)

        
    def set_params(self, **kwargs):
        '''Set model configuration parameters'''

        if len(kwargs) > 0:
            self.changed = True
            self.p.update(kwargs)


    def get_statistic(self, var, stat='avg'):
        '''Return statistic of spatial grid

        Parameters
        ----------
        var : str
            Name of spatial grid
        stat : str
            Name of statistic (avg, sum, var, min or max)

        Returns
        -------
        numpy.ndarray
            Statistic of spatial grid

        '''

        if stat in ['min', 'max', 'sum', 'var']:
            return self.o[var][stat]
        elif stat == 'avg':
            return self.o[var]['sum'] / self.n
        elif stat == 'var':
            return (self.o[var]['var'] - self[var]['sum']**2 / self.n) / (self.n - 1)
        else:
            return None

        
    def get_var(self, var):
        '''Returns spatial grid, statistic or model configuration parameter

        Overloads the :func:`aeolis.model.AeoLiS.get_var()` function and
        extends it with the functionality to return statistics on
        spatial grids by adding a postfix to the variable name
        (e.g. Ct.avg). Supported statistics are avg, sum, var, min and
        max.

        Parameters
        ----------
        var : str
            Name of spatial grid or model configuration
            parameter. Spatial grid name can be extended with a
            postfix to request a statistic (.avg, .sum, .var, .min or
            .max).

        Returns
        -------
        np.ndarray or int, float, str or list
            Spatial grid, statistic or model configuration parameter

        Examples
        --------
        >>> # returns average sediment concentration
        ... model.get_var('Ct.avg')

        >>> # returns variance in wave height
        ... model.get_var('Hs.var')

        See Also
        --------
        aeolis.model.AeoLiS.get_var

        '''

        self.clear = True

        if '.' in var:
            var, stat = var.split('.')
            if self.o.has_key(var):
                return self.get_statistic(var, stat)

        return super(AeoLiSRunner, self).get_var(var)


    def initialize(self):
        '''Initialize model

        Overloads the :func:`aeolis.model.AeoLiS.initialize()` function, but
        also initializes output statistics.

        '''
        
        super(AeoLiSRunner, self).initialize()
        self.output_init()
        

    def update(self, dt=-1):
        '''Time stepping function

        Overloads the :func:`aeolis.model.AeoLiS.update()` function,
        but also updates output statistics and clears output
        statistics upon request.

        Parameters
        ----------
        dt : float, optional
            Time step in seconds.

        '''

        if self.clear or self.dt < -1:
            self.output_clear()
            self.clear = False
            
        super(AeoLiSRunner, self).update(dt=dt)
        self.output_update()
        
                    
    def write_params(self):
        '''Write updated model configuration to configuration file

        Creates a backup in case the model configration file already
        exists.

        See Also
        --------
        aeolis.io.backup

        '''

        if self.changed:
            io.backup(self.configfile)
            io.write_configfile(self.configfile, self.p)
            self.changed = False
                            
        
    def output_init(self):

        '''Initialize netCDF4 output file and output statistics dictionary'''

        self.p['output_vars'] = makeiterable(self.p['output_vars'])
        self.p['output_types'] = makeiterable(self.p['output_types'])
        
        netcdf.initialize(self.p['output_file'],
                          self.p['output_vars'],
                          self.p['output_types'],
                          self.s,
                          self.p,
                          self.dimensions())

        self.output_clear()


    def output_clear(self):
        '''Clears output statistics dictionary

        Creates a matrix for minimum, maximum, variance and summed
        values for each output variable and sets the time step counter
        to zero.

        '''
        
        for k in self.p['output_vars']:
            s = self.get_var_shape(k)
            self.o[k] = dict(min=np.zeros(s) + np.inf,
                             max=np.zeros(s) - np.inf,
                             var=np.zeros(s),
                             sum=np.zeros(s))

        self.n = 0


    def output_update(self):
        '''Updates output statistics dictionary

        Updates matrices with minimum, maximum, variance and summed
        values for each output variable with current spatial grid
        values and increases time step counter with one.

        '''
        
        for k in self.p['output_vars']:
            v = self.get_var(k).copy()
            if 'min' in self.p['output_types']:
                self.o[k]['min'] = np.minimum(self.o[k]['min'], v)
            if 'max' in self.p['output_types']:
                self.o[k]['max'] = np.minimum(self.o[k]['max'], v)
            if 'sum' in self.p['output_types'] or \
               'avg' in self.p['output_types'] or \
               'var' in self.p['output_types']:
                self.o[k]['sum'] = self.o[k]['sum'] + v
            if 'var' in self.p['output_types']:
                self.o[k]['var'] = self.o[k]['var'] + v**2
            
        self.n += 1


    def output_write(self):
        '''Appends output to netCDF4 output file

        If the time since the last output is equal or larger than the
        set output interval, append current output to the netCDF4
        output file. Computes the average and variance values based on
        available output statistics and clear output statistics
        dictionary.

        '''
        
        if self.t - self.tout >= self.p['output_times']:
            
            variables = {}
            for k in self.p['output_vars']:
                variables['time'] = self.t
                variables[k] = self.get_var(k).copy()
                for t in self.p['output_types']:
                    variables['%s.%s' % (k, t)] = self.get_statistic(k, t)

            netcdf.append(self.p['output_file'], variables)
        
            self.output_clear()
            self.tout = self.t


    def parse_callback(self, callback):
        '''Parses callback definition and returns function

        The callback function can be specified in two formats:

        - As a native Python function
        - As a string refering to a Python script and function,
          separated by a colon (e.g. ``example/callback.py:function``)

        Parameters
        ----------
        callback : str or function
            Callback definition

        Returns
        -------
        function
            Python callback function

        '''

        if isinstance(callback, str):
            if ':' in callback:
                fname, func = callback.split(':')
                if os.path.exists(fname):
                    mod = imp.load_source('callback', fname)
                    if hasattr(mod, func):
                        return getattr(mod, func)
        elif hasattr(callback, '__call__'):
            return callback
        elif callback is None:
            return callback

        logger.warn('Invalid callback definition [%s]' % callback)
        return None

                        
    def print_progress(self, fraction=.1, min_interval=1., max_interval=60.):
        '''Print progress to screen

        Parameters
        ----------
        fraction : float, optional
            Fraction of simulation at which to print progress (default: 10%)
        min_interval : float, optional
            Minimum time in seconds between subsequent progress prints (default: 1s)
        max_interval : float, optional
            Maximum time in seconds between subsequent progress prints (default: 60s)

        '''
        
        p = self.t / self.p['tstop']
        pr = np.round(p/fraction)*fraction
        
        t = time.time()
        interval = t - self.tlog

        if (np.mod(p, fraction) < .01 and self.plog != pr) or interval > max_interval:
            t1 = time.strftime('%H:%M:%S', time.gmtime(t-self.t0))
            t2 = time.strftime('%H:%M:%S', time.gmtime((t-self.t0) / p))
            t3 = time.strftime('%H:%M:%S', time.gmtime((t-self.t0) * (1. - p) / p))
            print '%5.1f%%   %s / %s / %s' % (p * 100., t1, t2, t3)
            self.tlog = time.time()
            self.plog = pr
            
        
    def print_params(self):
        '''Print model configuration parameters to screen'''
        
        maxl = np.max([len(par) for par in self.p.iterkeys()])
        fmt1 = '  %-%%ds = %%s' % maxl
        fmt2 = '  %-%%ds   %%s' % maxl

        print '**********************************************************'
        print 'PARAMETER SETTINGS                                        '
        print '**********************************************************'

        for par, val in sorted(self.p.iteritems()):
            if isiterable(val):
                if par.endswith('_file'):
                    print fmt1 % (par, '%s.txt' % par.replace('_file', ''))
                elif len(val) > 0:
                    print fmt1 % (par, io.print_value(val[0]))
                    for v in val[1:]:
                        print fmt2 % ('', io.print_value(v))
                else:
                    print fmt1 % (par, '')
            else:
                print fmt1 % (par, io.print_value(val))

        print '**********************************************************'
        print ''


    def print_stats(self):
        '''Print model run statistics to screen'''

        n_time = self.get_count('time')
        n_matrixsolve = self.get_count('matrixsolve')
        n_supplylim = self.get_count('supplylim')

        print ''
        print '**********************************************************'

        fmt = '%-20s : %s'
        print fmt % ('# time steps', io.print_value(n_time))
        print fmt % ('# matrix solves', io.print_value(n_matrixsolve))
        print fmt % ('# supply lim', io.print_value(n_supplylim))
        print fmt % ('avg. solves per step',
                           io.print_value(float(n_matrixsolve) / n_time))
        print fmt % ('avg. time step',
                           io.print_value(float(self.p['tstop']) / n_time))

        print '**********************************************************'
        print ''


class WindGenerator():
    '''Wind velocity time series generator

    Generates a random wind velocity time series with given mean and
    maximum wind speed, duration and time resolution. The wind
    velocity time series is generated using a Markov Chain Monte Carlo
    (MCMC) approach based on a Weibull distribution. The wind time
    series can be written to an AeoLiS-compatible wind input file
    assuming a constant wind direction of zero degrees.

    The command-line function ``aeolis-wind`` is available that uses
    this class to generate AeoLiS wind input files.

    Examples
    --------
    >>> wind = WindGenerator(mean_speed=10.).generate(duration=24*3600.)
    >>> wind.write_time_series('wind.txt')
    >>> wind.plot()
    >>> wind.hist()

    See Also
    --------
    aeolis.cmd.wind

    '''
    
    # source:
    # http://www.lutralutra.co.uk/2012/07/02/simulating-a-wind-speed-time-series-in-python/


    def __init__(self,
                 mean_speed=9.0,
                 max_speed=30.0,
                 dt=60.,
                 n_states=30,
                 shape=2.,
                 scale=2.):
        
        self.mean_speed=mean_speed
        self.max_speed=max_speed
        self.n_states=n_states

        self.t=0.
        self.dt=dt
        
        # setup matrix
        n_rows = n_columns = n_states                             
        self.bin_size = float(max_speed)/n_states
        
        # weibull parameters
        weib_shape=shape
        weib_scale=scale*float(mean_speed)/np.sqrt(np.pi);
        
        # wind speed bins
        self.bins = np.arange(self.bin_size/2.0,
                              float(max_speed) + self.bin_size/2.0,
                              self.bin_size)
        
        # distribution of probabilities, normalised
        fdpWind = self.weibullpdf(self.bins, weib_scale, weib_shape)
        fdpWind = fdpWind / sum(fdpWind)
        
        # decreasing function
        G = np.empty((n_rows, n_columns,))
        for x in range(n_rows):
            for y in range(n_columns):
                G[x][y] = 2.0**float(-abs(x-y))
            
        # initial value of the P matrix
        P0 = np.diag(fdpWind)
        
        # initital value of the p vector
        p0 = fdpWind
        
        P, p = P0, p0
        rmse = np.inf
        while rmse > 1e-10:
            pp = p
            r = self.matmult4(P,self.matmult4(G,p))
            r = r/sum(r)
            p = p+0.5*(p0-r)
            P = np.diag(p)
            
            rmse = np.sqrt(np.mean((p - pp)**2))
            
        N=np.diag([1.0/i for i in self.matmult4(G,p)])
        MTM=self.matmult4(N,self.matmult4(G,P))
        self.MTMcum = np.cumsum(MTM,1)
        
        
    def __getitem__(self, s):
        return np.asarray(self.wind_speeds[s])
        
        
    def generate(self, duration=3600.):
        
        # initialise series
        self.state = 0
        self.states = []
        self.wind_speeds = []
        self.randoms1 = []
        self.randoms2 = []

        self.update()
        self.t = 0.
        
        while self.t < duration:
            self.update()

        return self


    def update(self):
        r1 = np.random.uniform(0,1)
        r2 = np.random.uniform(0,1)
        
        self.randoms1.append(r1)
        self.randoms2.append(r2)
        
        self.state = next(j for j,v in enumerate(self.MTMcum[self.state]) if v > r1)
        self.states.append(self.state)
        self.wind_speeds.append(self.bins[self.state] - 0.5 + r2 * self.bin_size)
        
        self.t += self.dt
        
        
    def get_time_series(self):
        u = np.asarray(self.wind_speeds)
        t = np.arange(len(u)) * self.dt
        
        return t, u


    def write_time_series(self, fname):
        t, u = self.get_time_series()
        M = np.concatenate((np.asmatrix(t),
                            np.asmatrix(u),
                            np.zeros((1, len(t)))), axis=0).T

        np.savetxt(fname, M)

    
    def plot(self):
        t, u = self.get_time_series()
        
        fig, axs = plt.subplots(figsize=(10,4))
        axs.plot(t, u

    , '-k')
        axs.set_ylabel('wind speed [m/s]')
        axs.set_xlabel('time [s]')
        axs.set_xlim((0, np.max(t)))
        axs.grid()

        return fig, axs
    
    
    def hist(self):
        fig, axs = plt.subplots(figsize=(10,4))
        axs.hist(self.wind_speeds, bins=self.bins, normed=True, color='k')
        axs.set_xlabel('wind speed [m/s]')
        axs.set_ylabel('occurence [-]')
        axs.grid()

        return fig, axs    


    @staticmethod
    def weibullpdf(data, scale, shape):
        return [(shape/scale)
                * ((x/scale)**(shape-1))
                * np.exp(-1*(x/scale)**shape)
                for x in data]


    @staticmethod
    def matmult4(m, v):
        return [reduce(operator.add, map(operator.mul,r,v)) for r in m]
