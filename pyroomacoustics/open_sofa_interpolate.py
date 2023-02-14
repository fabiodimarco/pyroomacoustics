import numpy as np
import scipy

try:
    import sofa

    has_sofa = True
except ImportError:
    has_sofa = False

import collections
import math
from timeit import default_timer as timer

from scipy.fft import fft, fftfreq, ifft
from scipy.interpolate import griddata
from scipy.signal import decimate
from scipy.spatial import KDTree, SphericalVoronoi, cKDTree

from .doa import GridSphere, cart2spher, fibonnaci_spherical_sampling, spher2cart
from .utilities import requires_matplotlib

RegularGrid = collections.namedtuple("RegularGrid", ["azimuth", "colatitude"])


def fibonacci_sphere(samples):
    """
    Creates a uniform fibonacci sphere.

    This version has some point at the pole which leads
    to a less *uniform* sampling. We should switch to
    fibonnaci_spherical_sampling

    Parameter
    ---------
    samples : (int)
        Points on the sphere

    Return
    --------
    Points : (np.array) shape(Points) = [no_of_points * 3]
        Cartesian coordinates of the points

    """

    points = []
    phi = math.pi * (3.0 - math.sqrt(5.0))  # golden angle in radians

    i = np.arange(samples)
    y = 1 - i / float(samples - 1) * 2
    radius = np.sqrt(1 - y * y)
    theta = phi * i
    x = np.cos(theta) * radius
    z = np.sin(theta) * radius

    return np.array([x, y, z])


def cal_sph_basis(theta, phi, degree):  # theta_target,phi_target
    """
    Calculate a spherical basis matrix

    Parameters
    -----------
    theta: array_like
       Phi (Azimuth) spherical coordinates of the SOFA file / fibonacci sphere
    phi: array_like
       Theta (Colatitude) spherical coordinates of the SOFA file / fibonacci sphere
    degree:(int)
       spherical harmonic degree

    Return
    ------
    Ysh (np.array) shape(Ysh) = no_of_nodes * (degree + 1)**2
        Spherical harmonics basis matrix

    """

    no_of_nodes = theta.shape[0]
    Ysh = np.zeros((len(theta), (degree + 1) ** 2), dtype=np.complex_)

    ny0 = 1
    for j in range(no_of_nodes):
        for i in range(degree + 1):
            m = np.linspace(0, i, int((i - 0) / 1.0 + 1), endpoint=True, dtype=int)
            sph_vals = [
                scipy.special.sph_harm(order, i, theta[j], phi[j]) for order in m
            ]
            cal_index_Ysh_positive_order = (ny0 + m) - 1

            Ysh[j, cal_index_Ysh_positive_order] = sph_vals
            if i > 0:
                m_neg = np.linspace(
                    -i, -1, int((-1 - -i) / 1.0 + 1), endpoint=True, dtype=int
                )
                sph_vals_neg = [
                    scipy.special.sph_harm(order, i, theta[j], phi[j])
                    for order in m_neg
                ]
                cal_index_Ysh_negative_order = (ny0 + m_neg) - 1

                Ysh[j, cal_index_Ysh_negative_order] = sph_vals_neg

            # Update index for next degree
            ny0 = ny0 + 2 * i + 2
        ny0 = 1
    return Ysh


def _detect_regular_grid(grid):
    """
    This function checks that the linearized azimuth/colatitude where sampled
    from a regular grid.

    It also checks that the azimuth are uniformly spread in [0, 2 * np.pi).
    The colatitudes can have arbitrary positions.

    Parameters
    ----------
    azimuth: numpy.ndarray (npoints,)
        The azimuth values in radian
    colatitude: numpy.ndarray (npoints,)
        The colatitude values in radian

    Returns
    -------
    regular_grid: dict["azimuth", "colatitude"] or None
        A dictionary with entries for the sorted distinct azimuth an colatitude values
        of the grid, if the points form a grid.
        Returns `None` if the points do not form a grid.
    """
    azimuth_unique = np.unique(grid.azimuth)
    colatitude_unique = np.unique(grid.colatitude)
    regular_grid = None
    if len(azimuth_unique) * len(colatitude_unique) == len(grid):
        # check that the azimuth are uniformly spread
        az_loop = np.insert(
            azimuth_unique, len(azimuth_unique), azimuth_unique[0] + 2 * np.pi
        )
        delta_az = np.diff(az_loop)
        if np.allclose(delta_az, 2 * np.pi / len(azimuth_unique)):
            # remake the grid from the unique points and check
            # that it matches the original
            A, C = np.meshgrid(azimuth_unique, colatitude_unique)
            regrid = np.column_stack([A.flatten(), C.flatten()])
            regrid = regrid[np.lexsort(regrid.T), :]
            ogrid = np.column_stack([grid.azimuth, grid.colatitude])
            ogrid = ogrid[np.lexsort(ogrid.T), :]
            if np.allclose(regrid, ogrid):
                regular_grid = RegularGrid(
                    azimuth=azimuth_unique, colatitude=colatitude_unique
                )

    return regular_grid


def _weighted_pinv(weights, Y, rcond=1e-2):
    return np.linalg.pinv(
        weights[:, None] * Y, rcond=rcond
    )  # rcond is inverse of the condition number


def calculation_pinv_voronoi_cells(Ysh, colatitude, colatitude_grid, len_azimuth_grid):
    """
    Weighted least square solution "Analysis and Synthesis of Sound-Radiation with Spherical Arrays: Franz Zotter Page 76"

    Calculation of pseudo inverse and voronoi cells for regular sampling in spherical coordinates

    Parameters
    -----------
    Ysh: (np.ndarray)
        Spherical harmonic basis matrix
    colatitude: (np.ndarray)
        The colatitudes of the measurements
    colatitude_grid: (int)
        the colatitudes of the grid lines
    len_azimuth_grid:
        The number of distinct azimuth values in the grid

    Returns:
    -------------------------------
    Ysh_tilda_inv : (np.ndarray)
        Weighted psuedo inverse of spherical harmonic basis matrix Ysh
    w_ : (np.ndarray)
        Weight on the original grid
    """
    # compute the areas of the voronoi regions of the grid analytically
    # assuming that the grid is regular in azimuth/colatitude
    res = (colatitude_grid[:-1] + colatitude_grid[1:]) / 2
    res = np.insert(res, len(res), np.pi)
    res = np.insert(res, 0, 0)
    w = -np.diff(np.cos(res)) * (2 * np.pi / len_azimuth_grid)
    w_dict = {t: ww for t, ww in zip(colatitude_grid, w)}

    # select the weights
    w_ = np.array([w_dict[col] for col in colatitude])
    w_ /= 4 * np.pi  # normalizing by unit sphere area

    return _weighted_pinv(w_, Ysh), w_


def calculation_pinv_voronoi_cells_general(Ysh, points):
    """
    Weighted least square solution "Analysis and Synthesis of Sound-Radiation with
    Spherical Arrays: Franz Zotter Page 76"

    Calculation of pseudo inverse and voronoi cells for arbitrary sampling of the sphere.

    Parameters
    -----------
    Ysh: (np.ndarray)
        Spherical harmonic basis matrix
    points: numpy.ndarray, (n_points, 3)
        The sampling points on the sphere

    Returns:
    -------------------------------
    Ysh_tilda_inv : (np.ndarray)
        Weighted pseudo inverse of spherical harmonic basis matrix Ysh
    w_ : (np.ndarray)
        Weight on the original grid
    """

    # The weights are the areas of the voronoi cells
    sv = SphericalVoronoi(points)
    w_ = sv.calculate_areas()
    w_ /= 4 * np.pi  # normalizing by unit sphere area

    Ysh_tilda_inv = np.linalg.pinv(
        w_[:, None] * Ysh, rcond=1e-2
    )  # rcond is inverse of the condition number

    return _weighted_pinv(w_, Ysh), w_


def spherical_interpolation(
    grid,
    impulse_responses,
    new_grid,
    spherical_harmonics_order=12,
    axis=-2,
    nfft=None,
):
    """
    Parameters
    ----------
    grid: pyroomacoustics.doa.GridSphere
        The grid of the measurements
    impulse_responses: numpy.ndarray, (..., n_measurements, ..., n_samples)
        The impulse responses to interpolate, the last axis is time and one other
        axis should have dimension matching the length of the grid. By default,
        it is assumed to be second from the end, but can be specified with the
        `axis` argument.
    new_grid: pyroomacoustics.doa.GridSphere
        Grid of points at which to interpolate
    spherical_harmonics_order: int
        The order of spherical harmonics to use for interpolation
    axis: int
        The axis of the grid in the impulse responses array
    nfft: int
        The length of the FFT to use for the interpolation (default ``n_samples``)
    """
    ir = np.swapaxes(impulse_responses, axis, -2)
    if nfft is None:
        nfft = ir.shape[-1]

    if len(grid) != ir.shape[-2]:
        raise ValueError(
            "The length of the grid should be the same as the number of impulse"
            f"responses provide (grid={len(grid)}, impulse response={ir.shape[-2]})"
        )

    # Calculate spherical basis for the original grid
    Ysh = cal_sph_basis(grid.azimuth, grid.colatitude, spherical_harmonics_order)

    # Calculate spherical basis for the target grid (fibonacci grid)
    Ysh_fibo = cal_sph_basis(
        new_grid.azimuth,
        new_grid.colatitude,
        spherical_harmonics_order,
    )

    # this will check if the points are on a regular grid.
    # If they are, then the azimuths and colatitudes of the grid
    # are returned
    regular_grid = _detect_regular_grid(grid)

    # calculate pinv and voronoi cells for least square solution for the whole grid
    if regular_grid is not None:
        Ysh_tilda_inv, w_ = calculation_pinv_voronoi_cells(
            Ysh,
            grid.colatitude,
            regular_grid.colatitude,
            len(regular_grid.azimuth),
        )
    else:
        Ysh_tilda_inv, w_ = calculation_pinv_voronoi_cells_general(
            Ysh, grid.cartesian.T
        )

    # Do the interpolation in the frequency domain

    # shape: (..., n_measurements, n_samples // 2 + 1)
    tf = np.fft.rfft(ir, axis=-1, n=nfft)

    g_tilda = w_[:, None] * tf

    gamma_full_scale = np.matmul(Ysh_tilda_inv, g_tilda)

    interpolated_original_grid = np.fft.irfft(
        np.matmul(Ysh, gamma_full_scale), n=nfft, axis=-1
    )
    interpolated_target_grid = np.fft.irfft(
        np.matmul(Ysh_fibo, gamma_full_scale), n=nfft, axis=-1
    )

    # restore the order of the axes
    interpolated_target_grid = np.swapaxes(interpolated_target_grid, -2, axis)
    interpolated_original_grid = np.swapaxes(interpolated_original_grid, -2, axis)

    return interpolated_target_grid, interpolated_original_grid


def DIRPAT_pattern_enum_id(DIRPAT_pattern_enum, source=False):
    """
    Assigns DIRPAT pattern enum to respective id present in the DIRPAT SOFA files.
    Works only for mic and source files


    """

    if source is not True:
        if "AKG_c480" in DIRPAT_pattern_enum:
            id = 0
        elif "AKG_c414K" in DIRPAT_pattern_enum:
            id = 1
        elif "AKG_c414N" in DIRPAT_pattern_enum:
            id = 2
        elif "AKG_c414S" in DIRPAT_pattern_enum:
            id = 3
        elif "AKG_c414A" in DIRPAT_pattern_enum:
            id = 4
        elif "EM_32" in DIRPAT_pattern_enum:
            id = int(DIRPAT_pattern_enum.split("_")[-1])
        else:
            raise ValueError("Please specifiy correct DIRPAT_pattern_enum for mic")
    else:
        if "Genelec_8020" in DIRPAT_pattern_enum:
            id = 0
        elif "Lambda_labs_CX-1A" in DIRPAT_pattern_enum:
            id = 1
        elif "HATS_4128C" in DIRPAT_pattern_enum:
            id = 2
        elif "Tannoy_System_1200" in DIRPAT_pattern_enum:
            id = 3
        elif "Neumann_KH120A" in DIRPAT_pattern_enum:
            id = 4
        elif "Yamaha_DXR8" in DIRPAT_pattern_enum:
            id = 5
        elif "BM_1x12inch_driver_closed_cabinet" in DIRPAT_pattern_enum:
            id = 6
        elif "BM_1x12inch_driver_open_cabinet" in DIRPAT_pattern_enum:
            id = 7
        elif "BM_open_stacked_on_closed_withCrossoverNetwork" in DIRPAT_pattern_enum:
            id = 8
        elif "BM_open_stacked_on_closed_fullrange" in DIRPAT_pattern_enum:
            id = 9
        elif "Palmer_1x12inch" in DIRPAT_pattern_enum:
            id = 10
        elif "Vibrolux_2x10inch" in DIRPAT_pattern_enum:
            id = 11
        else:
            raise ValueError("Please specifiy correct DIRPAT_pattern_enum for source")

    return id


def open_sofa_file(path, measurement_id, is_source, fs=16000):
    # Memo for notation of SOFA dimensions
    # From: https://www.sofaconventions.org/mediawiki/index.php/SOFA_conventions#AnchorDimensions
    # M 	number of measurements 	integer >0
    # R 	number of receivers or harmonic coefficients describing receivers 	integer >0
    # E 	number of emitters or harmonic coefficients describing emitters 	integer >0
    # N 	number of data samples describing one measurement 	integer >0
    # S 	number of characters in a string 	integer ≥0
    # I 	singleton dimension, constant 	always 1
    # C 	coordinate triplet, constant 	always 3

    # Open DirPat database
    if not has_sofa:
        raise ValueError(
            "The package 'sofa' needs to be installed to call this function. Install by doing `pip install sofa`"
        )

    file_sofa = sofa.Database.open(path)

    # read the mesurements
    IR_S = file_sofa.Data.IR.get_values()

    if is_source:
        # Receiver positions
        pos = file_sofa.Receiver.Position.get_values()
        pos_units = file_sofa.Receiver.Position.Units.split(",")

        # Look for source of specific type requested by user
        msr = IR_S[measurement_id, :, :]

    else:
        # Source positions
        pos = file_sofa.Source.Position.get_values()
        pos_units = file_sofa.Source.Position.Units.split(",")

        # Look for receiver of specific type requested by user
        msr = IR_S[:, measurement_id, :]

    # downsample the fir filter.
    msr = decimate(
        msr,
        int(round(file_sofa.Data.SamplingRate.get_values()[0] / fs)),
        axis=-1,
    )

    is_dirpat = path.name in [
        "Soundfield_ST450_CUBE.sofa",
        "AKG_c480_c414_CUBE.sofa",
        "Oktava_MK4012_CUBE.sofa",
        "LSPs_HATS_GuitarCabinets_Akustikmessplatz.sofa",
    ]
    if is_dirpat:
        # There is a bug in the DIRPAT measurement files where the array of
        # measurement locations were not flattened correctly
        pos_units[0:1] = "radian"
        if path.name == "LSPs_HATS_GuitarCabinets_Akustikmessplatz.sofa":
            pos_RS = np.reshape(pos, [36, -1, 3])
            pos = np.swapaxes(pos_RS, 0, 1).reshape([pos.shape[0], -1])
        else:
            pos_RS = np.reshape(pos, [30, -1, 3])
            pos = np.swapaxes(pos_RS, 0, 1).reshape([pos.shape[0], -1])

    azimuth = pos[:, 0]
    colatitude = pos[:, 1]
    distance = pos[:, 2]

    # All measurements should be in = radians phi [0,2*np.pi] , theta [0,np.pi]
    if not is_dirpat:
        if pos_units[0] == "degree":
            azimuth = np.deg2rad(azimuth)
        if pos_units[1] == "degree":
            colatitude = np.deg2rad(colatitude)

    if np.any(colatitude < 0.0):
        # it looks like the data is using elevation format
        colatitude = np.pi / 2.0 - colatitude

    # encapsulate the spherical grid points in a grid object
    grid = GridSphere(spherical_points=np.array([azimuth, colatitude]))

    return grid, distance, msr


class DIRPATInterpolate:

    """
    Intialization class performs the following functions
    Open DIRPAT Files
    Interpolate on fibonacci sphere
    Rotate the pattern w.r.t to the given azimuth(phi) [-pi, pi] and colatitude(theta) [0,pi] defines the orientation of the source or a receiver
    Reutrns response in the given direction w.r.t to the given angle of arrivals(microphones) / angle of departures(angle of departures)

    Parameters
    --------------
    path : (string)
        Path towards the specific DIRPAT file
    fs  : (int)
        Sampling frequency of the microphone or source FIR's filters , should be  <= simulation frequency
    DIRPAT_pattern_enum :  (string)
        The specific pattern in the DIRPAT files are associated with id's , presented in the github document
    source  : (Boolean)
        If the DIRPAT files represnts one of source or receiver's
    interpolate  : (Boolean)
        Interpolate the FIR filter or not
    no_of_points_fibo_sphere  : (int)
        How many points should be there on fibonnacci sphere
    azimuth_simulation  , colatitude_simulation : (int)
        Orientation of the source/mic in the simulation environment in degrees.

    """

    def __init__(
        self,
        path,
        fs,
        DIRPAT_pattern_enum=None,
        source=False,
        interp_order=None,
        interp_n_points=1000,
    ):
        self.path = path
        self.source = source

        self.fs = fs

        (
            self.grid_sofa,  # azimuth
            self.distance_sofa,
            self.impulse_responses_sofa,
        ) = open_sofa_file(
            path=self.path,
            measurement_id=DIRPAT_pattern_enum_id(DIRPAT_pattern_enum, source=source),
            is_source=source,
            fs=self.fs,
        )

        self.interpolation_order = interp_order

        if interp_order is not None:
            self.grid = GridSphere(
                cartesian_points=fibonacci_sphere(samples=interp_n_points)
            )

            self.impulse_responses, _ = spherical_interpolation(
                self.grid_sofa,
                self.impulse_responses_sofa,
                self.grid,
                spherical_harmonics_order=self.interpolation_order,
                axis=-2,
            )

        else:
            self.impulse_responses = self.impulse_responses_sofa
            self.grid = self.grid_sofa

        self.original_grid = self.grid
        self._build_kdtree()

    def _build_kdtree(self):
        self.kdtree = cKDTree(
            np.column_stack(
                (
                    self.grid.azimuth,
                    self.grid.colatitude,
                )
            )
        )

    def nearest_neighbour(self, azimuth, colatitude):
        """
        Calculates nearest neighbour for all the image source for the source and the receiver

        Retrives the FIR in frequency resolution according to the given index
        parameter. Index is found using query on kdtree based on incoming or
        outgoing angles of the image sources

        Parameters
        ----------
        azimuth: (np.ndarray)
             list of azimuth angles in radians [0,2 * pi]
        colatitude: (np.ndarray)
            list of colatitude angles in radians [0,pi]

        Returns
        -------
        np.ndarray
            FIR in frequency resolution for the particular query angle.
        """
        _, index = self.kdtree.query(np.column_stack((azimuth, colatitude)))
        return self.impulse_responses[index, :]

    def change_orientation(self, azimuth_change, colatitude_change, degrees=True):
        """
        Change of rotation without repeating the whole process only works for the sofa file where we are doing interpolation on fibo sphere
        Made for IWAENC.
        """

        if degrees:
            n_c = np.radians(colatitude_change)
            n_a = np.radians(azimuth_change)
        else:
            n_c = colatitude_change
            n_a = azimuth_change

        R_y = np.array(
            [[np.cos(n_c), 0, np.sin(n_c)], [0, 1, 0], [-np.sin(n_c), 0, np.cos(n_c)]]
        )
        R_z = np.array(
            [[np.cos(n_a), -np.sin(n_a), 0], [np.sin(n_a), np.cos(n_a), 0], [0, 0, 1]]
        )
        res = np.matmul(R_z, R_y)

        # rotate the grid and re-build the KD-tree
        self.grid = GridSphere(
            cartesian_points=np.matmul(res, self.original_grid.cartesian)
        )
        self._build_kdtree()

    def plot(self, freq_bin=0):
        """
        Plot 3D plots of the SOFA file , interpolated fibonnacci sphere and roatated fibo and sofa spheres.

        Display gain for a specified freq bin


        :Parameter (int) freq_bin:Bin number

        """
        import matplotlib.pyplot as plt

        fg = plt.figure(figsize=plt.figaspect(0.5))

        if self.interpolate:
            ax1 = fg.add_subplot(1, 4, 1, projection="3d")
            ax_ = ax1.scatter(
                self.grid_sofa.x,
                self.grid_sofa.y,
                self.grid_sofa.z,
                c=np.abs(self.freq_angles_fft[:, freq_bin]),
                s=100,
            )
            ax1.set_title("Mag|STFT| of one of the SOFA Files")
            fg.colorbar(ax_, shrink=0.5, aspect=5)

            ax2 = fg.add_subplot(1, 4, 2, projection="3d")
            ax_1 = ax2.scatter(
                self.sofa_x,
                self.sofa_y,
                self.sofa_z,
                c=np.abs(
                    fft(self.sh_coeffs_expanded_original_grid, axis=-1)[:, freq_bin]
                ),
                s=100,
            )
            ax2.set_title("Expanded Initial Grid")
            fg.colorbar(ax_1, shrink=0.5, aspect=5)

            ax3 = fg.add_subplot(1, 4, 4, projection="3d")
            ax_2 = ax3.scatter(
                self.rotated_fibo_points[0, :],
                self.rotated_fibo_points[1, :],
                self.rotated_fibo_points[2, :],
                c=np.abs(
                    fft(self.sh_coeffs_expanded_target_grid, axis=-1)[:, freq_bin]
                ),
                s=50,
            )
            ax3.set_title(
                "Interpolated And Rotated Target Grid Based On Analytic SOFA Receiver"
            )
            fg.colorbar(ax_2, shrink=0.5, aspect=5)

            ax4 = fg.add_subplot(1, 4, 3, projection="3d")
            ax_3 = ax4.scatter(
                self.points[:, 0],
                self.points[:, 1],
                self.points[:, 2],
                c=np.abs(
                    fft(self.sh_coeffs_expanded_target_grid, axis=-1)[:, freq_bin]
                ),
                s=50,
            )  # fibo_debug
            ax4.set_title("Interpolated Fibo Grid")
            fg.colorbar(ax_3, shrink=0.5, aspect=5)
        else:
            ax1 = fg.add_subplot(1, 2, 1, projection="3d")
            ax_ = ax1.scatter(
                self.sofa_x,
                self.sofa_y,
                self.sofa_z,
                c=np.abs(self.freq_angles_fft[:, freq_bin]),
                s=100,
            )
            ax1.set_title("Mag|STFT| of one of the SOFA Files")
            fg.colorbar(ax_, shrink=0.5, aspect=5)

            ax2 = fg.add_subplot(1, 2, 2, projection="3d")
            ax_1 = ax2.scatter(
                self.rotated_sofa_points[0, :],
                self.rotated_sofa_points[1, :],
                self.rotated_sofa_points[2, :],
                c=np.abs(self.freq_angles_fft[:, freq_bin]),
                s=100,
            )
            ax2.set_title("Expanded Initial Grid")
            fg.colorbar(ax_1, shrink=0.5, aspect=5)

        fg.tight_layout(pad=3.0)
        plt.show()

    @requires_matplotlib
    def plot_new(
        self, freq_bin=0, n_grid=100, ax=None, depth=False, offset=None, original=False
    ):
        import matplotlib.pyplot as plt
        from matplotlib import cm

        if original:
            cart = self.grid_sofa.cartesian.T
            length = np.abs(
                np.fft.rfft(self.impulse_responses_sofa, axis=-1)[:, freq_bin]
            )
        else:
            cart = self.grid.cartesian.T
            length = np.abs(np.fft.rfft(self.impulse_responses, axis=-1)[:, freq_bin])

        # regrid the data on a 2D grid
        g = np.linspace(-1, 1, n_grid)
        AZ, COL = np.meshgrid(
            np.linspace(0, 2 * np.pi, n_grid), np.linspace(0, np.pi, n_grid // 2)
        )
        # multiply by 0.99 to make sure the interpolation grid is inside the convex hull
        # of the original points, otherwise griddata returns NaN
        shrink_factor = 0.99
        while True:
            X = np.cos(AZ) * np.sin(COL) * 0.95
            Y = np.sin(AZ) * np.sin(COL) * 0.95
            Z = np.cos(COL) * 0.95
            grid = np.column_stack((X.flatten(), Y.flatten(), Z.flatten()))
            interp_len = griddata(cart, length, grid, method="linear")
            V = interp_len.reshape((n_grid // 2, n_grid))

            # there may be some nan
            if np.any(np.isnan(V)):
                shrink_factor *= 0.99
            else:
                break

        if ax is None:
            fig = plt.figure()
            ax = fig.add_subplot(1, 1, 1, projection="3d")

        # Colour the plotted surface according to the sign of Y.
        cmap = plt.cm.ScalarMappable(cmap=plt.get_cmap("coolwarm"))
        cmap.set_clim(0, V.max())

        if depth:
            X *= V
            Y *= V
            Z *= V

        surf = ax.plot_surface(
            X, Y, Z, facecolors=cmap.to_rgba(V), linewidth=0, antialiased=False
        )

        return ax
