import os
import pickle
import itertools
import multiprocessing
from functools import partial
from datetime import datetime
from time import time
from math import gcd
from typing import List, Iterator, Callable
from collections import namedtuple
import mpmath
import sympy
from sympy import lambdify
import numpy as np
from latex import generate_latex
from series_generators import create_series_from_compact_poly
from mobius import GeneralizedContinuedFraction, MobiusTransform, EfficientGCF
from convergence_rate import calculate_convergence


# intermediate result - coefficients of lhs transformation, and compact polynomials for seeding an and bn series.
Match = namedtuple('Match', 'lhs_coefs rhs_an_poly rhs_bn_poly')
FormattedResult = namedtuple('FormattedResult', 'LHS RHS GCF')


class GlobalHashTableInstance:
    def __init__(self):
        """
        python processes don't share memory. so when using multiprocessing the hash table will be duplicated.
        to try and avoid this, we initiate a global instance of the hash table.
        hopefully this will useful when running on linux (taking advantage of Copy On Write).
        this has not been tested yet on linux.
        on windows it has no effect when multiprocessing.
        """
        self.hash = {}
        self.name = ''


# global instance
hash_instance = GlobalHashTableInstance()


class LHSHashTable(object):

    def __init__(self, search_range_top, search_range_bottom, const_val, threshold) -> None:
        """
        hash table for LHS. storing values in the form of (ax + b)/(cx + d)
        :param search_range_top: range for values a,b.
        :param search_range_bottom: range for value c,d.
        :param const_val: constant for x.
        :param threshold: decimal threshold for comparison. in fact, the keys for hashing will be the first
                            -log_{10}(threshold) digits of the value. for example, if threshold is 1e-10 - then the
                            first 10 digits will be used as the hash key.
        """
        self.s = {}
        self.threshold = threshold
        for a in search_range_top:
            for b in search_range_top:
                for c in search_range_bottom:
                    for d in search_range_bottom:
                        if gcd(gcd(a, b), gcd(c, d)) != 1:  # don't store values that already exist
                            continue
                        denominator = c * const_val + d
                        numerator = a * const_val + b
                        if denominator == 0 or numerator == 0:  # don't store nan or 0.
                            continue
                        val = numerator / denominator
                        if mpmath.isnan(val) or mpmath.isinf(val):  # safety check
                            continue
                        if ((c + d) != 0) and mpmath.almosteq(val, ((mpmath.mpf(a) + mpmath.mpf(b)) / (c + d))):
                            # don't store values that are independent of the constant (e.g. rational numbers)
                            continue
                        key = int(val / self.threshold)
                        if key in self.s:
                            continue
                        self.s[key] = np.array([[a, b], [c, d]], dtype=object)  # store key and transformation

    def __contains__(self, item):
        """
        operator 'in'
        :param item: key
        :return: true of false
        """
        return item in self.s

    def __getitem__(self, item):
        """
        operator []
        :param item: key
        :return: transformation of x
        """
        return self.s[item]

    def __eq__(self, other):
        """
        operator ==
        :param other: other hash table.
        :return:
        """
        if type(other) != type(self):
            return False
        ret = self.threshold == other.threshold
        ret &= sorted(self.s.keys()) == sorted(other.s.keys())
        return ret

    def save(self, name):
        """
        save the hash table as file
        :param name: path for file.
        """
        if hash_instance.name != name:  # save to global instance.
            hash_instance.hash = self
            hash_instance.name = name
        with open(name, 'wb') as f:
            pickle.dump(self, f)

    @classmethod
    def load_from(cls, name):
        """
        load hash table from file (or global instance)
        :param name:
        :return:
        """
        if hash_instance.name == name:
            print('loading instance')
            return hash_instance.hash  # hopefully on linux this will not make a copy.
        else:
            with open(name, 'rb') as f:
                print('not loading instance')
                ret = pickle.load(f)
                hash_instance.hash = ret  # save in instance
                hash_instance.name = name
        return ret


class EnumerateOverGCF(object):
    def __init__(self, sym_constant, lhs_search_limit, saved_hash=''):
        """
        initialize search engine.
        basically, this is a 3 step procedure:
        1) load / initialize lhs hash table.
        2) first enumeration - enumerate over all rhs combinations, find hits in lhs hash table.
        3) refine results - take results from (2) and validate them to 100 decimal digits.
        :param sym_constant: sympy constant
        :param lhs_search_limit: range of coefficients for left hand side.
        :param saved_hash: path to saved hash.
        """
        self.threshold = 1e-10  # key length
        self.enum_dps = 50  # working decimal precision for first enumeration
        self.verify_dps = 2000  # working decimal precision for validating results.
        self.lhs_limit = lhs_search_limit
        self.const_sym = sym_constant
        try:
            self.const_val = lambdify((), sym_constant, modules="mpmath")
        except AttributeError:      # Hackish constant
            self.const_val = sym_constant.mpf_val
        self.create_an_series = create_series_from_compact_poly
        self.create_bn_series = create_series_from_compact_poly
        if saved_hash == '':
            print('no previous hash table given, initializing hash table...')
            with mpmath.workdps(self.enum_dps):
                start = time()
                self.hash_table = LHSHashTable(
                    range(self.lhs_limit + 1),  # a,b range (allow only non-negative)
                    range(-self.lhs_limit, self.lhs_limit + 1),  # c,d range
                    self.const_val(),  # constant
                    self.threshold)  # length of key
                end = time()
                print(f'that took {end-start}s')
        else:
            self.hash_table = LHSHashTable.load_from(saved_hash)

    @staticmethod
    def __number_of_elements(permutation_options: List[List]):
        res = 1
        for l in permutation_options:
            res *= len(l)
        return res

    @staticmethod
    def __create_series_list(coefficient_iter: Iterator,
                             series_generator: Callable[[List[int], int], List[int]]) -> [List[int], List[int]]:
        coef_list = list(coefficient_iter)
        # create a_n and b_n series fro coefficients.
        series_list = [series_generator(coef_list[i], 32) for i in range(len(coef_list))]
        # filter out all options resulting in '0' in any series term.
        series_filter = [0 not in an for an in series_list]
        series_list = list(itertools.compress(series_list, series_filter))
        coef_list = list(itertools.compress(coef_list, series_filter))
        return coef_list, series_list

    def __first_enumeration(self, poly_a: List[List], poly_b: List[List], print_results: bool):
        """
        this is usually the bottleneck of the search.
        we calculate general continued fractions of type K(bn,an). 'an' and 'bn' are polynomial series.
        these polynomials take the form of n(n(..(n*c_1 + c_0) + c_2)..)+c_k.
        poly parameters are a list of coefficients c_i. then the enumeration takes place on all possible products.
        for example, if poly_a is [[1,2],[2,3]], then the product polynomials are:
           possible [c0,c1] = { [1,2] , [1,3], [2,2], [2,3] }.
        this explodes exponentially -
        example: fs poly_a.shape = poly_b.shape = [3,5]     (2 polynomials of degree 2), then the number of
        total permutations is: (a poly possibilities) X (b poly possibilities) = (5X5X5) X (5X5X5) = 5**6
        we search on all possible gcf with polynomials defined by parameters, and try to find hits in hash table.
        :param poly_a: compact polynomial form of 'an' (list of lists)
        :param poly_b: compact polynomial form of 'an' (list of lists)
        :param print_results: if True print the status of calculation.
        :return: intermediate results (list of 'Match')
        """
        start = time()
        a_coef_iter = itertools.product(*poly_a)  # all coefficients possibilities for 'a_n'
        neg_poly_b = [[-i for i in b] for b in poly_b]  # for b_n include negative terms
        b_coef_iter = itertools.chain(itertools.product(*poly_b), itertools.product(*neg_poly_b))
        num_iterations = 2 * self.__number_of_elements(poly_b) * self.__number_of_elements(poly_a)
        size_b = 2 * self.__number_of_elements(poly_b)
        size_a = self.__number_of_elements(poly_a)

        counter = 0  # number of permutations passed
        results = []  # list of intermediate results

        if size_a > size_b:     # cache {bn} in RAM, iterate over an
            b_coef_list, bn_list = self.__create_series_list(b_coef_iter, self.create_bn_series)
            if print_results:
                print(f'created final enumerations filters after {time() - start}s')
            start = time()
            for a_coef in a_coef_iter:
                an = self.create_an_series(a_coef, 32)
                if 0 in an:
                    continue
                for bn_coef in zip(bn_list, b_coef_list):
                    gcf = EfficientGCF(an, bn_coef[0])  # create gcf from a_n and b_n
                    key = int(gcf.evaluate() / self.threshold)  # calculate hash key of gcf value
                    if key in self.hash_table:  # find hits in hash table
                        results.append(Match(self.hash_table[key], a_coef, bn_coef[1]))
                    if print_results:
                        counter += 1
                        if counter % 100000 == 0:  # print status.
                            print(f'passed {counter} out of {num_iterations}. found so far {len(results)} results')

        else:   # cache {an} in RAM, iterate over bn
            a_coef_list, an_list = self.__create_series_list(a_coef_iter, self.create_an_series)
            if print_results:
                print(f'created final enumerations filters after {time() - start}s')
            start = time()
            for b_coef in b_coef_iter:
                bn = self.create_bn_series(b_coef, 32)
                if 0 in bn:
                    continue
                for an_coef in zip(an_list, a_coef_list):
                    gcf = EfficientGCF(an_coef[0], bn)  # create gcf from a_n and b_n
                    key = int(gcf.evaluate() / self.threshold)  # calculate hash key of gcf value
                    if key in self.hash_table:  # find hits in hash table
                        results.append(Match(self.hash_table[key], an_coef[1], b_coef))
                    if print_results:
                        counter += 1
                        if counter % 100000 == 0:  # print status.
                            print(f'passed {counter} out of {num_iterations}. found so far {len(results)} results')

        if print_results:
            print(f'created results after {time() - start}s')
        return results

    def __refine_results(self, intermediate_results: List[Match], print_results=True):
        """
        validate intermediate results to 100 digit precision
        :param intermediate_results:  list of results from first enumeration
        :param print_results: if true print status.
        :return: final results.
        """
        results = []
        counter = 0
        n_iterations = len(intermediate_results)
        for r in intermediate_results:
            counter += 1
            if (counter % 10) == 0 and print_results:
                print('passed {} permutations out of {}. found so far {} matches'.format(
                    counter, n_iterations, len(results)))
            t = MobiusTransform(r.lhs_coefs)
            try:
                val = t(self.const_val())
                if mpmath.isinf(val) or mpmath.isnan(val):  # safety
                    continue
                if mpmath.almosteq(val, t(1), 1 / (self.verify_dps // 20)):
                    # don't keep results that are independent of the constant
                    continue
            except ZeroDivisionError:
                continue

            # create a_n, b_n with huge length, calculate gcf, and verify result.
            an = self.create_an_series(r.rhs_an_poly, 1000)
            bn = self.create_bn_series(r.rhs_bn_poly, 1000)
            gcf = EfficientGCF(an, bn)
            val_str = mpmath.nstr(val, 100)
            rhs_str = mpmath.nstr(gcf.evaluate(), 100)
            if val_str == rhs_str:
                results.append(r)
        return results

    def __get_formatted_results(self, results: List[Match]) -> List[FormattedResult]:
        ret = []
        for r in results:
            an = self.create_an_series(r.rhs_an_poly, 1000)
            bn = self.create_bn_series(r.rhs_bn_poly, 1000)
            print_length = max(max(len(r.rhs_an_poly), len(r.rhs_bn_poly)), 5)
            gcf = GeneralizedContinuedFraction(an, bn)
            t = MobiusTransform(r.lhs_coefs)
            sym_lhs = sympy.simplify(t.sym_expression(self.const_sym))
            ret.append(FormattedResult(sym_lhs, gcf.sym_expression(print_length), gcf))
        return ret

    def print_results(self, results: List[Match], latex=False):
        """
        pretty print the the results.
        :param results: list of final results as received from refine_results.
        :param latex: if True print in latex form, otherwise pretty print in unicode.
        """
        formatted_results = self.__get_formatted_results(results)
        for r in formatted_results:
            with mpmath.workdps(self.verify_dps):
                rate = calculate_convergence(r.GCF, lambdify((), r.LHS, 'mpmath')())
            if latex:
                result = sympy.Eq(r.LHS, r.RHS)
                print(f'$$ {sympy.latex(result)} $$')
            else:
                print('lhs: ')
                sympy.pprint(r.LHS)
                print('rhs: ')
                sympy.pprint(r.RHS)
            print("Converged with a rate of {} digits per term".format(mpmath.nstr(rate, 5)))

    def convert_results_to_latex(self, results: List[Match]):
        results_in_latex = []
        formatted_results = self.__get_formatted_results(results)
        for r in formatted_results:
            equation = sympy.Eq(r.LHS, r.RHS)
            results_in_latex.append(sympy.latex(equation))
        return results_in_latex

    def find_hits(self, poly_a: List[List], poly_b: List[List], print_results=True):
        """
        use search engine to find results (steps (2) and (3) explained in __init__ docstring)
        :param poly_a: explained in docstring of __first_enumeration
        :param poly_b: explained in docstring of __first_enumeration
        :param print_results: if true, pretty print results at the end.
        :return: final results.
        """
        with mpmath.workdps(self.enum_dps):
            if print_results:
                print('starting preliminary search...')
            start = time()
            # step (2)
            results = self.__first_enumeration(poly_a, poly_b, print_results)
            end = time()
            if print_results:
                print(f'that took {end - start}s')
        with mpmath.workdps(self.verify_dps*2):
            if print_results:
                print('starting to verify results...')
            start = time()
            refined_results = self.__refine_results(results, print_results)  # step (3)
            end = time()
            if print_results:
                print(f'that took {end - start}s')
        return refined_results


def multi_core_enumeration(sym_constant, lhs_search_limit, saved_hash, poly_a, poly_b, num_cores, splits_size,
                           create_an_series=None, create_bn_series=None, index=0):
    """
    function to run for each process. this also divides the work to tiles/
    :param sym_constant: sympy constant for search
    :param lhs_search_limit:  limit for hash table
    :param saved_hash: path to saved hash table
    :param poly_a: explained in docstring of __first_enumeration
    :param poly_b: explained in docstring of __first_enumeration
    :param num_cores: total number of cores used.
    :param splits_size: tile size for each process.
    we can think of the search domain as a n-d array with dim(poly_a) + dim(poly_b) dimensions.
    to split this efficiently we need the tile size. for each value in splits_size we take it as the tile size for a
    dimension of the search domain. for example, is split size is [4,5] then we will split the work in the first
    2 dimensions of the search domain to tiles of size [4,5].
    NOTICE - we do not verify that the tile size make sense to the number of cores used.
    :param index: index of core used.
    :param create_an_series: a custom function for creating a_n series with poly_a coefficients
    (default is create_series_from_compact_poly)
    :param create_bn_series: a custom function for creating b_n series with poly_b coefficients
    (default is create_series_from_compact_poly)
    :return: results
    """
    for s in range(len(splits_size)):
        if index == (num_cores - 1):
            poly_a[s] = poly_a[s][index * splits_size[s]:]
        else:
            poly_a[s] = poly_a[s][index * splits_size[s]:(index + 1) * splits_size[s]]
    enumerator = EnumerateOverGCF(sym_constant, lhs_search_limit, saved_hash)

    if create_an_series is not None:
        enumerator.create_an_series = create_an_series
    if create_bn_series is not None:
        enumerator.create_bn_series = create_bn_series

    results = enumerator.find_hits(poly_a, poly_b, index == 0)
    enumerator.print_results(results, latex=True)
    
    results_in_latex = enumerator.convert_results_to_latex(results)
    generate_latex(file_name=f'results/{datetime.now().strftime("%m-%d-%Y--%H-%M-%S")}', eqns=results_in_latex)

    return results


def multi_core_enumeration_wrapper(sym_constant, lhs_search_limit, poly_a, poly_b, num_cores, manual_splits_size=None,
                                   saved_hash=None, create_an_series=None, create_bn_series=None):
    """
    a wrapper for enumerating using multi processing.
    :param sym_constant: sympy constant for search
    :param lhs_search_limit: limit for hash table
    :param poly_a: explained in docstring of __first_enumeration
    :param poly_b: explained in docstring of __first_enumeration
    :param num_cores: total number of cores to be used.
    :param manual_splits_size: manuals tiling (explained in docstring of multi_core_enumeration)
    by default we will split the work only along the first dimension. so the tile size will be
    [dim0 / n_cores, . , . , . , rest of dimensions].
    passing this manually can be useful for a large number of cores.
    :param saved_hash: path to saved hash table file if exists.
    :param create_an_series: a custom function for creating a_n series with poly_a coefficients
    (default is create_series_from_compact_poly)
    :param create_bn_series: a custom function for creating b_n series with poly_b coefficients
    (default is create_series_from_compact_poly)
    :return: results.
    """
    print(locals())
    if (saved_hash is None) or (not os.path.isfile(saved_hash)):
        if saved_hash is None:  # if no hash table given, build it here.
            saved_hash = 'tmp_hash.p'
        enumerator = EnumerateOverGCF(sym_constant, lhs_search_limit)
        enumerator.hash_table.save(saved_hash)  # and save it to file (and global instance)
    else:
        if os.name != 'nt':  # if creation of process uses 'Copy On Write' we can benefit from it by
            # loading the hash table to memory here.
            EnumerateOverGCF(sym_constant, lhs_search_limit, saved_hash)

    if manual_splits_size is None:  # naive work split
        manual_splits_size = [len(poly_a[0]) // num_cores]

    # built function for processes
    func = partial(multi_core_enumeration, sym_constant, lhs_search_limit, saved_hash, poly_a, poly_b, num_cores,
                   manual_splits_size, create_an_series, create_bn_series)

    if num_cores == 1:  # don't open child processes
        results = func(0)
        print(f'found {len(results)} results!')
    else:
        pool = multiprocessing.Pool(num_cores)
        results = pool.map(func, range(num_cores))
        print(f'found {sum([len(results[i]) for i in range(num_cores)])} results!')

    return results
