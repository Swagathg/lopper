#!/usr/bin/python3

#/*
# * Copyright (c) 2019,2020 Xilinx Inc. All rights reserved.
# *
# * Author:
# *       Bruce Ashfield <bruce.ashfield@xilinx.com>
# *
# * SPDX-License-Identifier: BSD-3-Clause
# */

import struct
import sys
import types
import unittest
import os
import getopt
import re
import subprocess
import shutil
from pathlib import Path
from pathlib import PurePath
from io import StringIO
import contextlib
import importlib
from lopper import Lopper
from lopper import LopperFmt
import lopper
from libfdt import Fdt, FdtSw, FdtException, QUIET_NOTFOUND, QUIET_ALL
import libfdt

def is_compat( node, compat_string_to_test ):
    if re.search( "openamp,domain-v1", compat_string_to_test):
        return process_domain
    return ""

# tests for a bit that is set, going fro 31 -> 0 from MSB to LSB
def check_bit_set(n, k):
    if n & (1 << (k)):
        return True

    return False

# all the logic for applying a openamp domain to a device tree.
# this is a really long routine that will be broken up as more examples
# are done and it can be propery factored out.
def process_domain( tgt_node, sdt, verbose=0 ):
    if verbose:
        print( "[INFO]: cb: process_domain( %s, %s, %s )" % (tgt_node, sdt, verbose))

    tgt_domain = sdt.node_abspath( tgt_node )
    domain_properties = sdt.node_properties_as_dict( tgt_domain, verbose )

    if 'cpus' in domain_properties.keys():
        cpu_prop_values = domain_properties['cpus']
    else:
        print( "[ERROR]: domain node does not have a cpu link" )
        sys.exit(1)

    # the cpu handle is element 0
    cpu_prop = cpu_prop_values[0]
    cpu_node = sdt.FDT.node_offset_by_phandle( cpu_prop )

    if verbose:
        print( "[INFO]: cpu prop phandle: %s" % cpu_prop )
        print( "[INFO]: cpu node: %s" % cpu_node )

    ## We  need to delete any other nodes that have "compatible = cpus,cluster"
    ## and are Not the ones we just found in the chosen node. All we have is a phandle
    ##  so we need to:
    ##   1) find the nodes that are compatible with the cpus,cluster
    ##   2) check their phandle
    ##   3) delete if it isn't the one we just got
    xform_path = "/"
    prop = "cpus,cluster"
    code = """
p = prop_get( %%FDT%%, %%NODE%%, \"compatible\" )
if p and "%%%prop%%%" in p:
    ph = getphandle( %%FDT%%, %%NODE%% )
    if ph != %%%phandle%%%:
        %%TRUE%%
    else:
        %%FALSE%%
else:
    %%FALSE%%
"""
    code = code.replace( "%%%prop%%%", prop )
    code = code.replace( "%%%phandle%%%", str( cpu_prop ) )

    if verbose:
        print( "[INFO]: filtering on:\n------%s-------\n" % code )

    # the action will be taken if the code block returns 'true'
    Lopper.node_filter( sdt, xform_path, "delete", code, verbose )

    # we must re-find the domain node, since its numbering may have
    # changed due to the node_filter deleting things
    tgt_node = sdt.node_find( tgt_domain )

    # lets track any nodes that are referenced by access parameters. We use this
    # for a second patch to drop any nodes that are not accessed, and hence should
    # be removed
    node_access_tracker = {}
    # we want to track child nodes of "/", if they are of type simple-bus AND they are
    # referenced by a <access> value in the domain
    node_access_tracker['/'] = [ "/", "simple-bus" ]

    # "access" is a list of tuples: phandles + flags
    accesss_list = []
    if 'access' in domain_properties.keys():
        access_list = domain_properties['access']

    if not access_list:
        if verbose:
            print( "[INFO]: no access list found, skipping ..." )
    else:
        #print( "[INFO]: converted access list: %s" % access_list )

        # although the access list is decoded as a list, it is actually tuples, so we need
        # to get every other entry as a phandle, not every one.
        for ph in access_list[::2]:
            #ph = int(ph_hex, 16)
            #print( "processing %s" % ph )
            anode = sdt.FDT.node_offset_by_phandle( ph )
            node_type = sdt.property_get( anode, "compatible" )
            node_name = sdt.FDT.get_name( anode )
            node_parent = sdt.FDT.parent_offset(anode,QUIET_NOTFOUND)
            if re.search( "simple-bus", node_type ):
                if verbose > 1:
                    print( "[INFO]: access is a simple-bus (%s), leaving all nodes" % node_name)
                # refcount the bus
                full_name = sdt.node_abspath( anode )
                sdt.node_ref_inc( full_name )
            else:
                # The node is *not* a simple bus, so we must do more processing

                # a) If the node parent is something other than zero, the node is nested, so
                #    we have to do more processing.
                #    Note: this should be recursive eventually, but for now, we keep it simple
                # print( "node name: %s node parent: %s" % (node_name, node_parent) )
                if node_parent:
                    parent_node_type = sdt.node_type( node_parent )
                    # TODO: could wrap the get_name call as well in a lopper static function
                    parent_node_name = sdt.FDT.get_name( node_parent )
                    node_grand_parent = sdt.FDT.parent_offset(node_parent,QUIET_NOTFOUND)
                    if not parent_node_type:
                        # is it a special name ? .. if it is, we'll give it a type to normalize the
                        # code below
                        if re.search( "reserved-memory", parent_node_name ):
                            parent_node_type = "reserved-memory"
                        else:
                            # if there's no type and no special name, we need to bail
                            continue

                    # if the parent is a simple bus, then something withing the bus had an
                    # <access>. We need to refcount and delete anything that isn't accessed.
                    if re.search( "simple-bus", parent_node_type ):
                        # refcount the bus
                        full_name = sdt.node_abspath( node_parent )
                        sdt.node_ref_inc( full_name )

                        if not full_name in node_access_tracker:
                            node_access_tracker[full_name] = [ full_name, "*" ]

                        if verbose > 1:
                            print( "[INFO]: node parent is a simple-bus (%s), dropping sibling nodes" % parent_node_name)

                        full_name = sdt.node_abspath( anode )
                        sdt.node_ref_inc( full_name )

                    elif re.search( "reserved-memory", parent_node_type ):
                        if verbose > 1:
                            print( "[INFO]: reserved memory processing for: %s" % node_name)

                        full_name = sdt.node_abspath( node_parent )
                        if not full_name in node_access_tracker:
                            node_access_tracker[full_name] = [ full_name, "*" ]

                        # Increment a reference to the current node, since we've added the parent node
                        # to a list of nodes that we'll use to check for referenced children later. Anything
                        # with no reference, will be removed.
                        full_name = sdt.node_abspath( anode )
                        sdt.node_ref_inc( full_name )

        for n, value in node_access_tracker.values():
            # xform_path is the path to the node that was tracked, so this is a potential
            # delete to any children of that node, that haven't been accessed. If you
            # started from / .. you could delete a lot of nodes by mistake, so be careful!

            xform_path = n
            if value == "*":
                code = """
p = refcount( %%SDT%%, %%NODENAME%% )
if p <= 0:
    %%TRUE%%
else:
    %%FALSE%%
"""
            else:
                prop = value
                code = """
p = prop_get( %%FDT%%, %%NODE%%, \"compatible\" )
if p and "%%%prop%%%" in p:
    p = refcount( %%SDT%%, %%NODENAME%% )
    if p <= 0:
        %%TRUE%%
    else:
        %%FALSE%%
else:
    %%FALSE%%
"""
            code = code.replace( "%%%prop%%%", prop )

            if verbose:
                print( "[INFO]: filtering on:\n------%s-------\n" % code )

            # the action will be taken if the code block returns 'true'
            if n == "/":
                Lopper.node_filter( sdt, "/", "delete", code, verbose )
            else:
                Lopper.node_filter( sdt, n + "/", "delete", code, verbose )

            # we must re-find the domain node, since its numbering may have
            # changed due to the node_filter deleting things
            tgt_node = sdt.node_find( tgt_domain )

    # we must re-find the domain node, since its numbering may have
    # changed due to the node_filter deleting things
    tgt_node = sdt.node_find( tgt_domain )
    # TODO: we might need to refresh our property dict here, but for now, we don't.

    if 'memory' in domain_properties.keys():
        memory_hex = sdt.property_get( tgt_node, "memory", LopperFmt.COMPOUND, LopperFmt.HEX )
        memory_int = domain_properties["memory"]

    # This may be moved to the top of the domain process and then when we are
    # processing cpus and bus nodes, we can apply the memory to ranges <>, etc,
    # and modify them accordingly.
    if verbose > 1:
        print( "[INFO]: memory property: %s" % memory_hex )

    # 1) find if there's a top level memory node
    memory_node = sdt.node_find( "/memory" )
    if memory_node:
        if verbose:
            print( "[INFO]: memory node found (%s), modifying to match domain memory" % memory_node )

        # 2) modify that memory property to match the node we have here
        # memprop_old = sdt.FDT.getprop(memory_node, 'reg' )
        # num_bits = len(memprop_old)
        # a = 0
        # b = 1
        # c = 0
        # d = 1
        # val = a.to_bytes(4,byteorder='big') + b.to_bytes(4,byteorder='big') + c.to_bytes(4,byteorder='big') + d.to_bytes(4,byteorder='big')

        # TODO: this seems to be shortening up the system memory node. Check to see
        #       if the openamp node is being propery interpreted

        sdt.property_set( memory_node, 'reg', memory_int )
        # temp: keeping the raw call, in case the lopper utility has issues.
        # sdt.FDT.setprop(memory_node, 'reg', Lopper.encode_byte_array(memory_int))

    return True

