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
import tempfile
from enum import Enum
import atexit

import libfdt
from libfdt import Fdt, FdtSw, FdtException, QUIET_NOTFOUND, QUIET_ALL

lopper_directory = os.path.dirname(os.path.realpath(__file__))

# For use in encode/decode routines
class LopperFmt(Enum):
    SIMPLE = 1
    COMPOUND = 2
    HEX = 3
    DEC = 4
    STRING = 5
    MULTI_STRING = 5

@contextlib.contextmanager
def stdoutIO(stdout=None):
    old = sys.stdout
    if stdout is None:
        stdout = StringIO()
        sys.stdout = stdout
        yield stdout
        sys.stdout = old

def at_exit_cleanup():
    if device_tree:
        device_tree.cleanup()
    else:
        pass

class Lopper:
    @staticmethod
    def node_find( fdt, node_prefix ):
        """Finds a node by its prefix

        Args:
            fdt (fdt): flattened device tree object
            node_prefix (string): device tree path

        Returns:
            int: node number if successful, otherwise -1
        """
        try:
            node = fdt.path_offset( node_prefix )
        except:
            node = -1

        return node

    @staticmethod
    def node_type( fdt, node_offset, verbose=0 ):
        """Utility function to get the "type" of a node

	A small wrapper around the compatible property, but we can use this
        instead of directly getting compatible, since if we switch formats or if
        we want to infer anything based on the name of a node, we can hide it in
        this routine

        Args:
            fdt (fdt): flattened device tree object
            node_offset (int): node number
            verbose (int): verbose output level

        Returns:
            string: compatible string of the node if successful, otherwise ''
        """
        rt = Lopper.prop_get( fdt, node_offset, "compatible" )

        return rt

    @staticmethod
    def node_parent_type( fdt, node_name="", node_offset=0 ):
        """Get the type of a node's parent

	Uses either the node_name or node_offset to find the type of a
        parent node. This is used to discover if node's parent is of a
        particular type (i.e. simple_bus) and use it as a trigger for
        special processing. 

        Args:
            fdt (fdt): flattened device tree object
            node_name (string): name of the node
            node_offset (int): node number / offset

        Returns:
            string: type of the parent node if successful, otherwise ''
        """

        parent_node_type = ""
        if node_offset:
            # a node number was passed, use that as our first choice
            node_parent = fdt.parent_offset( node_offset, QUIET_NOTFOUND)
        else:
            # a node name (path) was passed. Lookup the offset of it, and
            # then find the parent.
            try:
                node_offset = fdt.path_offset( node_name )
            except:
                node_parent = 0

            node_parent = fdt.parent_offset( node_offset, QUIET_NOTFOUND)

        if node_parent:
            parent_node_type = Lopper.prop_get( fdt, node_parent, "compatible" )
            parent_node_name = fdt.get_name( node_parent )

        return parent_node_type

    @staticmethod
    def node_find_by_name( fdt, node_name, starting_node = 0 ):
        """Finds a node by its name (not path)

        Searches for a node by its name, and returns the offset of that same node
        Note: use this when you don't know the full path of a node

        Args:
            fdt (fdt): flattened device tree object
            node_name (string): name of the node
            starting_node (int): node to use as the search starting point

        Returns:
            int: offset of the node if successful, otherwise -1
	"""

        nn = starting_node
        # short circuit the search if they are looking for /
        if node_name == "/":
            depth = -1
        else:
            depth = 0
        matching_node = -1
        while depth >= 0:
            nn_name = fdt.get_name(nn)
            if nn_name:
                if re.search( nn_name, node_name ):
                    matching_node = nn
                    depth = -1
                else:
                    nn, depth = fdt.next_node(nn, depth, (libfdt.BADOFFSET,))
            else:
                nn, depth = fdt.next_node(nn, depth, (libfdt.BADOFFSET,))

        return matching_node

    @staticmethod
    def node_prop_check( fdt, node_name, property_name ):
        """Check if a node contains a property

        Boolean check to see if a node contains a property.

        The node name does not need to be a full path or path prefix, since
        the node will be searched starting at the root node, which means that
        a non-unique node name could match multiple nodes.

        Args:
            fdt (fdt): flattened device tree object
            node_name (string): name of the node
            property_name (string): name of the property to check

        Returns:
            bool: True if the node has the property, otherwise False
        """

        node = Lopper.node_find_by_name( fdt, node_name )
        try:
            fdt.getprop( property_name )
        except:
            return False

        return True

    @staticmethod
    def node_add( fdt_dest, node_full_path, create_parents = True, verbose = 0 ):
        """Add an empty node to a flattended device tree

        Creates a new node in a flattened devide tree at a given path. If
        desired a node structure (aka parents) will be created as part of
        adding the node at the specified path.

        Args:
            fdt_dest (fdt): flattened device tree object
            node_full_path (string): fully specified path (and name) of the node to create
            create_parents (bool,optional): Should parent nodes be created. Default is True.
                True: create parents as required, False: error if parents are missing
	    verbose (int,optional): verbosity level. default is 0.

        Returns:
            int: The node offset of the created node, if successfull, otherwise -1
        """

        prev = -1
        for p in os.path.split( node_full_path ):
            n = Lopper.node_find( fdt_dest, p )
            if n < 0:
                if create_parents:
                    prev = fdt_dest.add_subnode( prev, p )
            else:
                prev = n

        return prev

    @staticmethod
    def node_properties_as_dict( fdt, node, verbose=0 ):
        """Create a dictionary populated with the nodes properties.

        Builds a dictionary that is propulated with a node's properties as
        the keys, and their values. Used as a utility routine to avoid
        multiple calls to check if a property exists, and then to fetch its
        value.

        Args:
            fdt (fdt): flattened device tree object
            node (int or string): either a node number or node path
	    verbose (int,optional): verbosity level. default is 0.

        Returns:
            dict: dictionary of the properties, if successfull, otherwise and empty dict
        """
    
        prop_dict = {}

        # is the node a number ? or do we need to look it up ?
        node_number = -1
        node_path = ""
        try:
            node_number = int(node)
            node_path = Lopper.node_abspath( fdt, node )
        except ValueError:
            node_number = Lopper.node_find( fdt, node )
            node_path = node

        if node_number == -1:
            print( "[ERROR]: could not find node %s" % node_path )
            return prop_dict

        prop_list = Lopper.property_list( fdt, node_path )
        for p in prop_list:
            property_val = Lopper.prop_get( fdt, node_number, p )
            if not property_val:
                property_val = Lopper.prop_get( fdt, node_number, p, LopperFmt.COMPOUND )

            prop_dict[p] = property_val

        return prop_dict

    @staticmethod
    def node_copy_from_path( fdt_source, node_source_path, fdt_dest, node_full_dest, verbose=0 ):
        """Copies a node from one FDT to another

        Copies a node between flattened device trees. The node (and
        properties) will be copied to the specified target device tree and
        path (ensure that a node does not already exist at the destination
        path).

        This routine is a wrapper around node_copy(), and will create a
        parent node structure in the destination fdt as required.

        Args:
            fdt_source (fdt): source flattened device tree object
            node_source_path: source device tree node path (fully specified)
            fdt_dest (fdt): destination flattened device tree object
            node_full_dest: destination device tree path for copied node (fully specified)
	    verbose (int,optional): verbosity level. default is 0.

        Returns:
            bool: True if the node was copied, otherise, False
        """

        if verbose > 1:
            print( "[DBG ]: node_copy_from_path: %s -> %s" % (node_source_path, node_full_dest) )

        node_to_copy = Lopper.node_find( fdt_source, node_source_path )
        node_dest_path = os.path.dirname( node_full_dest )
        node_dest_name = os.path.basename( node_full_dest )

        if node_dest_path == "/":
            node_dest_parent_offset = 0
        else:
            # non root dest
            node_dest_parent_offset = Lopper.node_find( fdt_dest, node_dest_path )
            if node_dest_parent_offset == -1:
                node_dest_parent_offset = Lopper.node_add( fdt_dest, node_dest_path )
                if node_dest_parent_offset <= 0:
                    print( "[ERROR]: could not create new node" )
                    sys.exit(1)

        if node_to_copy:
            return Lopper.node_copy( fdt_source, node_to_copy, fdt_dest, node_dest_parent_offset, verbose )

        return False

    @staticmethod
    def node_copy( fdt_source, node_source_offset, fdt_dest, node_dest_parent_offset, verbose=0 ):
        """Copies a node from one FDT to another

        Copies a node between flattened device trees. The node (and
        properties) will be copied to the specified target device tree and
        path (ensure that a node does not already exist at the destination
        path).

	Note: the destination node parent must exist before calling this routine

        Properties are iterated, decoded and then copied (encoded) to the
        destination node. As such, the copies are limited by the
        decode/encode capabilities. If properties do not look correct in the
        copy, the decode/encode routines need to be checked.

        Args:
            fdt_source (fdt): source flattened device tree object
            node_source_offset: source device tree node offset
            fdt_dest (fdt): destination flattened device tree object
            node_dest_parent_offset: destination device parent node
	    verbose (int,optional): verbosity level. default is 0.

        Returns:
            bool: True if the node was copied, otherise, False
        """
    
        old_depth = -1
        depth = 0
        nn = node_source_offset
        newoff = node_dest_parent_offset
        while depth >= 0:
            nn_name = fdt_source.get_name(nn)
            try:
                copy_added_node_offset = fdt_dest.add_subnode( newoff, nn_name )
            except:
                print( "[ERROR]: could not create subnode for node copy" )
		# TODO: could return False and make this only exit on --werror
                sys.exit(1)

            prop_offset = fdt_dest.subnode_offset( newoff, nn_name )

            if verbose > 2:
                print( "" )
                print( "[DBG+]: properties for: %s" % fdt_source.get_name(nn) )

            prop_list = []
            poffset = fdt_source.first_property_offset(nn, QUIET_NOTFOUND)
            while poffset > 0:
                prop = fdt_source.get_property_by_offset(poffset)

                # we insert, not append. So we can flip the order of way we are
                # discovering the properties
                prop_list.insert( 0, prop )

                if verbose > 2:
                    print( "            prop name: %s" % prop.name )
                    print( "            prop raw: %s" % prop )

                if verbose > 2:
                    prop_val = Lopper.property_value_decode( prop, 0 )
                    if not prop_val:
                        prop_val = Lopper.property_value_decode( prop, 0, LopperFmt.COMPOUND )
                    print( "            prop decoded: %s" % prop_val )
                    print( "            prop type: %s" % type(prop_val))
                    print( "" )

                poffset = fdt_source.next_property_offset(poffset, QUIET_NOTFOUND)

            # loop through the gathered properties and copy them over. We are reversing
            # the order of the way we iterated them, due to the way that setprop inserts
            # at zero every time. If we don't flip the order the copied node will have
            # them in the opposite order!
            for prop in prop_list:
                # TODO: should these be using the wrapper functions ?  we sometimes misidentify.
                prop_val = Lopper.property_value_decode( prop, 0 )
                if not prop_val:
                    prop_val = Lopper.property_value_decode( prop, 0, LopperFmt.COMPOUND )

                Lopper.prop_set( fdt_dest, prop_offset, prop.name, prop_val )

            old_depth = depth
            nn, depth = fdt_source.next_node(nn, depth, (libfdt.BADOFFSET,))

            # we need a new offset fo the next time through this loop (but only if our depth
            # changed)
            if depth >= 0 and old_depth != depth:
                newoff = fdt_dest.subnode_offset( newoff, nn_name )

        return True

    @staticmethod
    def node_abspath( fdt, nodeid ):
        """Get the absolute (fully specified) path of a node

        Args:
            fdt (fdt): flattened device tree object
            nodeid: device tree node offset

        Returns:
            string: node path, if successful, otherwise ""
        """

        node_id_list = [nodeid]
        p = fdt.parent_offset(nodeid,QUIET_NOTFOUND)
        while p != 0:
            node_id_list.insert( 0, p )
            p = fdt.parent_offset(p,QUIET_NOTFOUND)

        retname = ""
        for id in node_id_list:
            retname = retname + "/" + fdt.get_name( id )

        return retname

    @staticmethod
    def nodes_with_property( fdt, match_propname, match_regex="",
                             start_path="/", include_children=True ):
        """Get a list of nodes with a particular property

        Searches a device tree and returns a list of nodes that contain
        a given property.

        Matching is done by the existence of a property name in a node.

        If a match_regex is passed, then the value of the property is
        tested against the regex. If there's a match, then the node is
        added to the list.

        Args:
            fdt (fdt): source flattened device tree to search
            match_propname (string): target property name
            match_regex (string,optional): property value match regex. Default is ""
            start_path (string,optional): starting path in the device tree. Default is "/"
            include_children (bool,optional): should child nodes be searched. Default is True.

        Returns:
            list: list of matching nodes if successful, otherwise an empty list
        """

        # node_list = []
        depth = 0
        ret_nodes = []
        if start_path != "/":
            node = Lopper.node_find_by_name( fdt, start_path )
        else:
            node = 0

        while depth >= 0:
            prop_val = Lopper.prop_get( fdt, node, match_propname )
            if prop_val:
                if match_regex:
                    if re.search( match_regex, prop_val ):
                        ret_nodes.append(node)
                else:
                    if match_propname == prop.name:
                        if not node in ret_nodes:
                            ret_nodes.append(node)

            node, depth = fdt.next_node(node, depth, (libfdt.BADOFFSET,))

        return ret_nodes

    @staticmethod
    def write_sdt( sdt_to_write, output_filename, overwrite=True, verbose=0 ):
        """Write a system device tree to a file

        A wrapper to easily write a system device tree that uses the output
        filename to determine if a module should be used to write the output.

        If the output format is .dts or .dtb, Lopper takes care of writing
        the output.  If it is an unrecognized output type, the available
        assist modules are queried for compatibility. If there is a
        compatible assist, it is called to write the file, otherwise, and
        error is raised.

        Args:
            fdt (fdt): source flattened device tree to search
            match_propname (string): target property name
            match_regex (string,optional): property value match regex. Default is ""
            start_path (string,optional): starting path in the device tree. Default is "/"
	    include_children (bool,optional): should child nodes be searched. Default is True.

        Returns:
            list: list of matching nodes if successful, otherwise an empty list
        """

	# TODO: the search for output modules can't be hardcoded like this, it
	#       should be based on a data input via a 'lop'
        if re.search( ".cdo", output_filename ):
            cb_funcs = sdt_to_write.find_module_compatible_func( 0, "xlnx,output,cdo" )
            if cb_funcs:
                for cb_func in cb_funcs:
                    try:
			# TODO: the first parameter 'node', shouldn't always be the root (0)
                        if not cb_func( 0, sdt_to_write, sdt_to_write.verbose ):
                            print( "[WARNING]: the assist returned false, check for errors ..." )
                    except Exception as e:
                        print( "[WARNING]: assist %s failed" % cb_func )
                        exc_type, exc_obj, exc_tb = sys.exc_info()
                        fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
                        print(exc_type, fname, exc_tb.tb_lineno)
            else:
                print( "[INFO]: no compatible assist found, skipping" )

        elif re.search( ".dtb", output_filename ) or re.search( ".dts", output_filename ):
            Lopper.write_fdt( sdt_to_write.FDT, output_filename, overwrite, verbose )
        else:
            print( "[ERROR]: could not detect output format" )
            sys.exit(1)

    @staticmethod
    def write_fdt( fdt_to_write, output_filename, overwrite=True, verbose=0 ):
        # switch on the output format. i.e. we may want to write commands/drivers
        # versus dtb .. and the logic to write them out should be loaded from
        # separate implementation files

	# TODO: add / generalize the output assist loading to allow this to
	#       call assists
        if re.search( ".dtb", output_filename ):
            if verbose:
                print( "[INFO]: dtb output format detected, writing %s" % output_filename )

            byte_array = fdt_to_write.as_bytearray()

            if verbose:
                print( "[INFO]: writing output dtb: %s" % output_filename )

            o = Path(output_filename)
            if o.exists() and not overwrite:
                print( "[ERROR]: output file %s exists and force overwrite is not enabled" % output_filename )
                sys.exit(1)

            with open(output_filename, 'wb') as w:
                w.write(byte_array)

        elif re.search( ".dts", output_filename ):
            if verbose:
                print( "[INFO]: dts format detected, writing %s" % output_filename )

            o = Path(output_filename)
            if o.exists() and not overwrite:
                print( "[ERROR]: output file %s exists and force overwrite is not enabled" % output_filename )
                sys.exit(1)

            # write the device tree to a temporary dtb
            fp = tempfile.NamedTemporaryFile()
            byte_array = fdt_to_write.as_bytearray()
            with open(fp.name, 'wb') as w:
                w.write(byte_array)

            Lopper.dtb_dts_export( fp.name, output_filename )

            # close the temp file so it is removed
            fp.close()
        else:
            print( "[ERROR]: could not detect output format" )
            sys.exit(1)


    #
    #  - a more generic way to modify/filter nodes
    #
    #  - node_prefix can be "" and we start at the root
    #  - action can be "delete" "report" "whitelist" "blacklist" ... TBD
    #  - test_op varies based on the action being taken
    #
    # TODO: replace action with an enumerated type, not the current
    #       free form string.
    @staticmethod
    def node_filter( sdt, node_prefix, action, test_cmd, verbose=0 ):
        fdt = sdt.FDT
        if verbose:
            print( "[NOTE]: filtering nodes root: %s" % node_prefix )

        if not node_prefix:
            node_prefix = "/"

        try:
            node_list = Lopper.get_subnodes( fdt, node_prefix )
        except:
            node_list = []
            if verbose:
                print( "[WARN]: no nodes found that match prefix %s" % node_prefix )

        # make a list of safe functions
        safe_list = ['Lopper.prop_get', 'Lopper.getphandle', 'Lopper.node_filter', 'Lopper.refcount', 'verbose', 'print']

        # this should work, but isn't resolving the local vars, so we have to add them again in the
        # loop below.
        # references: https://stackoverflow.com/questions/701802/how-do-i-execute-a-string-containing-python-code-in-python
        #             http://code.activestate.com/recipes/52217-replace-embedded-python-code-in-a-string-with-the-/
        safe_dict = dict([ (k, locals().get(k, None)) for k in safe_list ])
        safe_dict['len'] = len
        safe_dict['print'] = print
        safe_dict['prop_get'] = Lopper.prop_get
        safe_dict['getphandle'] = Lopper.getphandle
        safe_dict['node_filter'] = Lopper.node_filter
        safe_dict['refcount'] = Lopper.refcount
        safe_dict['fdt'] = fdt
        safe_dict['sdt'] = sdt
        safe_dict['verbose'] = verbose

        if verbose > 1:
            print( "[INFO]: filter: base safe dict: %s" % safe_dict )
            print( "[INFO]: filter: node list: %s" % node_list )

        for n in node_list:
            # build up the device tree node path
            node_name = node_prefix + n
            node = fdt.path_offset(node_name)
            #print( "---------------------------------- node name: %s" % fdt.get_name( node ) )
            prop_list = Lopper.property_list( fdt, node_name )
            #print( "---------------------------------- node props name: %s" % prop_list )

            # Add the current node (n) to the list of safe things
            # NOTE: might not be required
            # safe_list.append( 'n' )
            # safe_list.append( 'node_name' )

            # add any needed builtins back in
            safe_dict['n'] = n
            safe_dict['node'] = node
            safe_dict['node_name' ] = node_name

            # search and replace any template options in the cmd. yes, this is
            # only a proof of concept, you'd never do this like this in the end.
            tc = test_cmd
            tc = tc.replace( "%%FDT%%", "fdt" )
            tc = tc.replace( "%%SDT%%", "sdt" )
            tc = tc.replace( "%%NODE%%", "node" )
            tc = tc.replace( "%%NODENAME%%", "node_name" )
            tc = tc.replace( "%%TRUE%%", "print(\"true\")" )
            tc = tc.replace( "%%FALSE%%", "print(\"false\")" )

            if verbose > 2:
                print( "[DBG+]: filter node cmd: %s" % tc )

            # TODO: return values need to replace the stdout format of
            #       true and false
            with stdoutIO() as s:
                try:
                    exec(tc, {"__builtins__" : None }, safe_dict)
                except Exception as e:
                    print("Something wrong with the code: %s" % e)

            if verbose > 2:
                print( "[DBG+] stdout was: %s" % s.getvalue() )
            if "true" in s.getvalue():
                # TODO: add more actions
                if "delete" in action:
                    if verbose:
                        print( "[INFO]: deleting node %s" % node_name )
                    fdt.del_node( node, True )
            else:
                pass

    @staticmethod
    def node_dump( fdt, node_path, children=False, verbose=0 ):
        nn = fdt.path_offset( node_path )
        old_depth = -1
        depth = 0
        newoff = 0
        indent = 0
        while depth >= 0:
            nn_name = fdt.get_name(nn)

            outstring = nn_name + " {"
            print( outstring.rjust(len(outstring)+indent," " ))

            prop_list = []
            poffset = fdt.first_property_offset(nn, QUIET_NOTFOUND)
            while poffset > 0:
                prop = fdt.get_property_by_offset(poffset)
                prop_list.append(prop.name)

                prop_val = Lopper.property_value_decode( prop, 0 )
                if not prop_val:
                    prop_val = Lopper.property_value_decode( prop, 0, LopperFmt.COMPOUND, LopperFmt.HEX )

                outstring = "{0} = {1}".format( prop.name, prop_val )
                print( outstring.rjust(len(outstring)+indent+4," " ))

                if verbose > 2:
                    outstring = "prop type: {}".format(type(prop_val))
                    print( outstring.rjust(len(outstring)+indent+12," " ))
                    outstring = "prop name: {}".format( prop.name )
                    print( outstring.rjust(len(outstring)+indent+12," " ))
                    outstring = "prop raw: {}".format( prop )
                    print( outstring.rjust(len(outstring)+indent+12," " ))

                poffset = fdt.next_property_offset(poffset, QUIET_NOTFOUND)

            if children:
                old_depth = depth
                nn, depth = fdt.next_node(nn, depth, (libfdt.BADOFFSET,))

                # we need a new offset fo the next time through this loop (but only if our depth
                # changed)
                if depth >= 0 and old_depth != depth:
                    pass
                else:
                    outstring = "}"
                    print( outstring.rjust(len(outstring)+indent," " ))

                indent = depth + 3
            else:
                depth = -1

        print( "}" )


    @staticmethod
    def remove_node_if_not_compatible( fdt, node_prefix, compat_string ):
        if verbose:
            print( "[NOTE]: removing incompatible nodes: %s %s" % (node_prefix, compat_string) )

        node_list = []
        node_list = Lopper.get_subnodes( fdt, node_prefix )
        #print( "node list: %s" % node_list )
        for n in node_list:
            # build up the device tree node path
            node_name = node_prefix + n
            node = fdt.path_offset(node_name)
            # print( "node name: %s" % fdt.get_name( node ) )
            prop_list = Lopper.property_list( fdt, node_name )
            # print( "prop list: %s" % prop_list )
            if "compatible" in prop_list:
                # print( "This node has a compatible string!!!" )
                prop_value = fdt.getprop( node, 'compatible' )
                # split on null, since if there are multiple strings in the compat, we
                # need them to be separate
                vv = prop_value[:-1].decode('utf-8').split('\x00')
                # print( "prop_value as strings: %s" % vv )
                if not compat_string in vv:
                    if verbose:
                        print( "[INFO]: deleting node %s" % node_name )
                    fdt.del_node( node, True )

    # source: libfdt tests
    @staticmethod
    def get_subnodes(fdt, node_path):
        """Read a list of subnodes from a node

        Args:
        node_path: Full path to node, e.g. '/subnode@1/subsubnode'

        Returns:
        List of subnode names for that node, e.g. ['subsubnode', 'ss1']
        """
        subnode_list = []
        node = fdt.path_offset(node_path)
        offset = fdt.first_subnode(node, QUIET_NOTFOUND)
        while offset > 0:
            name = fdt.get_name(offset)
            subnode_list.append(name)
            offset = fdt.next_subnode(offset, QUIET_NOTFOUND)

        return subnode_list

    # source: libfdt tests
    @staticmethod
    def property_list( fdt, node_path ):
        """Read a list of properties from a node

        Args:
        node_path: Full path to node, e.g. '/subnode@1/subsubnode'

        Returns:
        List of property names for that node, e.g. ['compatible', 'reg']
        """
        prop_list = []
        node = fdt.path_offset(node_path)
        poffset = fdt.first_property_offset(node, QUIET_NOTFOUND)
        while poffset > 0:
            prop = fdt.get_property_by_offset(poffset)
            prop_list.append(prop.name)
            poffset = fdt.next_property_offset(poffset, QUIET_NOTFOUND)

        return prop_list

    #
    # reference routine to walk (and gather) a list of all nodes in
    # the tree.
    #
    @staticmethod
    def walk_nodes( FDT ):
        node_list = []
        node = 0
        depth = 0
        while depth >= 0:
            node_list.append([depth, FDT.get_name(node)])
            node, depth = FDT.next_node(node, depth, (libfdt.BADOFFSET,))

        # print( "node list: %s" % node_list )

    @staticmethod
    def dtb_dts_export( dtb, outfilename="", verbose=0 ):
        """writes a dtb to a file or to stdout as a dts

        Args:
        dtb: a compiled device tree
        outfilename: the output filename (stdout is used if empty)
        verbose: extra debug info

        Returns:
        The return value of executing dtc to dump the dtb to dts
        """
        dtcargs = (os.environ.get('LOPPER_DTC') or shutil.which("dtc")).split()
        dtcargs += (os.environ.get("STD_DTC_FLAGS") or "").split()
        dtcargs += (os.environ.get("LOPPER_DTC_BFLAGS") or "").split()
        if outfilename:
            dtcargs += ["-o", "{0}".format(outfilename)]
        dtcargs += ["-I", "dtb", "-O", "dts", dtb]

        if verbose:
            print( "[INFO]: dumping dtb: %s" % dtcargs )

        result = subprocess.run(dtcargs, check = False, stderr=subprocess.PIPE )

        return result

    # utility command to get a phandle (as a number) from a node
    @staticmethod
    def getphandle( fdt, node_number ):
        prop = fdt.get_phandle( node_number )
        return prop

    # utility command to get a property (as a string) from a node ftype can be
    # "simple" or "compound". A string is returned for simple, and a list of
    # properties for compound
    @staticmethod
    def prop_get( fdt, node_number, property_name, ftype=LopperFmt.SIMPLE, encode=LopperFmt.DEC ):
        try:
            prop = fdt.getprop( node_number, property_name )
            if prop:
                # TODO: both these conditions are the same .. either one goes, or one changes
                if ftype == LopperFmt.SIMPLE:
                    val = Lopper.property_value_decode( prop, 0, ftype, encode )
                else:
                    val = Lopper.property_value_decode( prop, 0, ftype, encode )
            else:
                val = ""
        except:
            val = ""

        return val

    @staticmethod
    def prop_set( fdt_dest, node_number, prop_name, prop_val, ftype=LopperFmt.SIMPLE ):
        try:
            prop_val_converted = int(prop_val,0)
            # if it works, that's our new prop_val. This covers the case where
            # a string is passed in, but it is really just a single number.
            # note: we may need to consult "ftype" in the future so the caller
            # can override this automatical conversion
            prop_val = prop_val_converted
        except:
            # do nothing. let propval go through as whatever it was
            pass

        # we have to re-encode based on the type of what we just decoded.
        if type(prop_val) == int:
            # this seems to break some operations, but a variant may be required
            # to prevent overflow situations
            # if sys.getsizeof(prop_val) >= 32:
            if sys.getsizeof(prop_val) > 32:
                fdt_dest.setprop_u64( node_number, prop_name, prop_val )
            else:
                fdt_dest.setprop_u32( node_number, prop_name, prop_val )
        elif type(prop_val) == str:
            fdt_dest.setprop_str( node_number, prop_name, prop_val )
        elif type(prop_val) == list:
            # list is a compound value, or an empty one!
            if len(prop_val) >= 0:
                try:
                    bval = Lopper.encode_byte_array_from_strings(prop_val)
                except:
                    bval = Lopper.encode_byte_array(prop_val)

                fdt_dest.setprop( node_number, prop_name, bval)
        else:
            print( "[WARNING]; uknown type was used" )


    @staticmethod
    def dt_compile( dts_file, i_files, includes, force_overwrite=False ):
        output_dtb = ""

        # TODO: might need to make 'dts_file' absolute for the cpp call below
        dts_filename = os.path.basename( dts_file )
        dts_dirname = os.path.dirname( dts_file )
        dts_filename_noext = os.path.splitext(dts_filename)[0]

        #
        # step 1: preprocess the file with CPP (if available)
        #

        # NOTE: we are putting the .pp file into the same directory as the
        #       system device tree. Without doing this, dtc cannot resolve
        #       labels from include files, and will throw an error. If we get
        #       into a mode where the system device tree's directory is not
        #       writeable, then we'll have to either copy everything or look
        #       into why dtc can't handle the split directories and include
        #       files.
        preprocessed_name = "{0}/{1}.pp".format(dts_dirname,dts_filename)

        includes += dts_dirname
        includes += " "
        includes += os.getcwd()

        ppargs = (os.environ.get('LOPPER_CPP') or shutil.which("cpp")).split()
        # Note: might drop the -I include later
        ppargs += "-nostdinc -I include -undef -x assembler-with-cpp ".split()
        ppargs += (os.environ.get('LOPPER_PPFLAGS') or "").split()
        for i in includes.split():
            ppargs.append("-I{0}".format(i))
        ppargs += ["-o", preprocessed_name, dts_file]
        if verbose:
            print( "[INFO]: preprocessing dts_file: %s" % ppargs )
        # TODO: could grab the output and dump it on error
        subprocess.run( ppargs, check = True )

        # step 2: compile the dtb
        #         dtc -O dtb -o test_tree1.dtb test_tree1.dts
        isoverlay = False
        output_dtb = "{0}.{1}".format(dts_filename, "dtbo" if isoverlay else "dtb")

        # make sure the dtb is not on disk, since it won't be overwritten by
        # default.
        if os.path.exists( output_dtb ):
            if not force_overwrite:
                print( "[ERROR]: output dtb (%s) exists and -f was not passed" % output_dtb )
                sys.exit(1)
            os.remove( output_dtb )

        dtcargs = (os.environ.get('LOPPER_DTC') or shutil.which("dtc")).split()
        dtcargs += (os.environ.get( 'LOPPER_DTC_FLAGS') or "").split()
        if isoverlay:
            dtcargs += (os.environ.get("LOPPER_DTC_OFLAGS") or "").split()
        else:
            dtcargs += (os.environ.get("LOPPER_DTC_BFLAGS") or "").split()
        for i in includes.split():
            dtcargs += ["-i", i]
        dtcargs += ["-o", "{0}".format(output_dtb)]
        dtcargs += ["-I", "dts", "-O", "dtb", preprocessed_name ]
        if verbose:
            print( "[INFO]: compiling dtb: %s" % dtcargs )

        result = subprocess.run(dtcargs, check = False, stderr=subprocess.PIPE )
        if result is not 0:
            # force the dtb, we need to do processing
            dtcargs += [ "-f" ]
            if verbose:
                print( "[INFO]: forcing dtb generation: %s" % dtcargs )

            result = subprocess.run(dtcargs, check = False, stderr=subprocess.PIPE )
            if result.returncode is not 0:
                print( "[ERROR]: unable to (force) compile %s" % dtcargs )
                sys.exit(1)



        # cleanup: remove the .pp file
        if not save_temps:
            os.remove( preprocessed_name )

        # if we got here, and for some reason the output_dtb does not exist, we should
        # zero the name and return "" instead.
        output_file = Path(output_dtb)
        try:
            output_file_path = output_file.resolve()
        except FileNotFoundError:
            output_dtb = ""

        return output_dtb

    @staticmethod
    def input_file_type(infile):
        return PurePath(infile).suffix

    @staticmethod
    def encode_byte_array( values ):
        barray = b''
        for i in values:
            barray = barray + i.to_bytes(4,byteorder='big')
        return barray

    @staticmethod
    def encode_byte_array_from_strings( values ):
        barray = b''
        if len(values) > 1:
            for i in values:
                barray = barray + i.encode() + b'\x00'
        else:
            barray = barray + values[0].encode()

        return barray

    @staticmethod
    def refcount( sdt, nodename ):
        return sdt.node_ref( nodename )

    #
    # Parameters:
    #   - Property object from libfdt
    #   - poffset (property offset) [optional]
    #   - ftype: simple or compound
    #   - encode: <format> is optional, and can be: dec or hex. 'dec' is the default
    @staticmethod
    def property_value_decode( property, poffset, ftype=LopperFmt.SIMPLE, encode=LopperFmt.DEC, verbose=0 ):
        if verbose > 3:
            print( "[DBG+]: property_value_decode start ------> %s %s" % (property,ftype))

        # Note: these could also be nested.
        # Note: this is temporary since the decoding
        #       is sometimes wrong. We need to look at libfdt and see how they are
        #       stored so they can be unpacked better.
        if ftype == LopperFmt.SIMPLE:
            val = ""
            decode_msg = ""
            try:
                val = property.as_uint32()
                decode_msg = "(uint32): {0}".format(val)
            except:
                pass
            if not val and val != 0:
                try:
                    val = property.as_uint64()
                    decode_msg = "(uint64): {0}".format(val)
                except:
                    pass
            if not val and val != 0:
                try:
                    val = property.as_str()
                    decode_msg = "(string): {0}".format(val)
                except:
                    pass
            if not val and val != 0:
                try:
                    # this is getting us some false positives on multi-string. Need
                    # a better test
                    #val = property[:-1].decode('utf-8').split('\x00')
                    val = ""
                    decode_msg = "(multi-string): {0}".format(val)
                except:
                    pass

            if not val and val != 0:
                decode_msg = "** unable to decode value **"
        else:
            decode_msg = ""
            encode_calculated = encode
            val = []

            # if we have b'' (and empty array), return an empty []
            if not property:
                return val

            first_byte = property[0]
            last_byte = property[-1]

            # byte array encoded strings, start with a non '\x00' byte (i.e. a character), so
            # we test on that for a hint. If it is not \x00, then we try it as a string.
            # Note: we may also test on the last byte for a string terminator.
            if first_byte != 0:
                encode_calculated = LopperFmt.STRING

            # TODO: we shouldn't need these repr() wrappers around the enums, but yet
            #       it doesn't seem to work on the calculated variable without them
            if repr(encode_calculated) == repr(LopperFmt.STRING):
                try:
                    val = property[:-1].decode('utf-8').split('\x00')
                    decode_msg = "(multi-string): {0}".format(val)
                except:
                    encode_calculated = encode

            if ( repr(encode_calculated) == repr(LopperFmt.DEC)) or \
                   (repr(encode_calculated) == repr(LopperFmt.HEX)):
                try:
                    decode_msg = "(multi number)"
                    num_bits = len(property)
                    num_nums = num_bits // 4
                    start_index = 0
                    end_index = 4
                    short_int_size = 4
                    val = []
                    while end_index <= (num_nums * short_int_size):
                        short_int = property[start_index:end_index]
                        if repr(encode) == repr(LopperFmt.HEX):
                            converted_int = hex(int.from_bytes(short_int,'big',signed=False))
                        else:
                            converted_int = int.from_bytes(short_int,'big',signed=False)

                        start_index = start_index + short_int_size
                        end_index = end_index + short_int_size
                        val.append(converted_int)
                except:
                    decode_msg = "** unable to decode value **"


        if verbose > 3:
            print( "[DBG+]: decoding property: \"%s\" (%s) [%s] --> %s" % (property, poffset, property, decode_msg ) )

        return val


##
## SystemDeviceTree
##
##  - wraps a dts/dtb/fdt containing a system description
##  - manages and applies operations to the tree
##  - calls modules and assist functions for processing of that tree
##
##
class SystemDeviceTree:
    def __init__(self, sdt_file):
        self.dts = sdt_file
        self.dtb = ""
        self.lops = []
        self.modules = []
        self.verbose = 0
        self.node_access = {}
        self.dry_run = False
        self.assists = []
        self.output_file = ""
        self.cleanup_flag = True
        self.save_temps = False

    def setup(self, sdt_file, input_files, include_paths, assists=[], force=False):
        if self.verbose:
            print( "[INFO]: loading dtb and using libfdt to manipulate tree" )

        # check for required support applications
        support_bins = ["dtc", "cpp" ]
        for s in support_bins:
            if verbose:
                print( "[INFO]: checking for support binary: %s" % s )
            if not shutil.which(s):
                print( "[ERROR]: support application '%s' not found, exiting" % s )
                sys.exit(2)

        self.use_libfdt = True

        # self.FDT = libfdt.Fdt(open(self.dtb, mode='rb').read())
        current_dir = os.getcwd()

        lop_files = []
        sdt_files = []
        for ifile in input_files:
            if re.search( ".dts$", ifile ):
                # an input file is either a lopper operation file, or part of the
                # system device tree. We can check for compatibility to decide which
                # it is.
                with open(ifile) as f:
                    datafile = f.readlines()
                    found = False
                    for line in datafile:
                        if not found:
                            if re.search( "system-device-tree-v1,lop", line ):
                                lop_files.append( ifile )
                                found = True

                if not found:
                    sdt_files.append( ifile )
            elif re.search( ".dtb$", ifile ):
                lop_files.append( ifile )

        # is the sdt a dts ?
        if re.search( ".dts$", self.dts ):
            # do we have any extra sdt files to concatenate first ?
            fp = ""
            fpp = tempfile.NamedTemporaryFile( delete=False )
            # TODO: if the count is one, we shouldn't be doing the tmp file processing.
            if sdt_files:
                sdt_files.insert( 0, self.dts )

                # this block concatenates all the files into a single dts to
                # compile
                with open( fpp.name, 'wb') as wfd:
                    for f in sdt_files:
                        with open(f,'rb') as fd:
                            shutil.copyfileobj(fd, wfd)

                fp = fpp.name
            else:
                sdt_files.append( sdt_file )
                fp = sdt_file

            self.dtb = Lopper.dt_compile( fp, input_files, include_paths, force )
            self.FDT = libfdt.Fdt(open(self.dtb, mode='rb').read())

            fpp.close()
        else:
            self.dtb = sdt_file
            self.dts = ""

        if verbose:
            print( "" )
            print( "SDT summary:")
            print( "   system device tree: %s" % sdt_files )
            print( "   lops: %s" % lop_files )
            print( "   output: %s" % self.output_file )
            print( "" )

        # Individually compile the input files. At some point these may be
        # concatenated with the main SDT if dtc is doing some of the work, but for
        # now, libfdt is doing the transforms so we compile them separately
        for ifile in lop_files:
            if re.search( ".dts$", ifile ):
                lop = Lop( ifile )
                compiled_file = Lopper.dt_compile( lop.dts, "", include_paths, force )
                if not compiled_file:
                    print( "[ERROR]: could not compile file %s" % ifile )
                    sys.exit(1)
                lop.dtb = "{0}.{1}".format(ifile, "dtb")
                self.lops.append( lop )
            elif re.search( ".dtb$", ifile ):
                lop = Lop( ifile )
                lop.dts = ""
                lop.dtb = ifile
                self.lops.append( lop )

        for a in assists:
            inf = Path(a)
            if not inf.exists():
                print( "[ERROR]: cannot find assist %s" % a )
                sys.exit(2)
            self.assists.append(a)

        self.load_assists()

    def cleanup( self ):
        # remove any .dtb and .pp files we created
        if self.cleanup and not self.save_temps:
            try:
                os.remove( self.dtb )
            except:
                # doesn't matter if the remove failed, it means it is
                # most likely gone
                pass

        # note: we are not deleting assists .db files, since they
        #       can actually be binary blobs passed in. We are also
        #       not cleaning up the concatenated compiled. pp file, since
        #       it is created with mktmp()

    def write( self, outfilename ):
        byte_array = self.FDT.as_bytearray()

        if self.verbose:
            print( "[INFO]: writing output dtb: %s" % outfilename )

        with open(outfilename, 'wb') as w:
            w.write(byte_array)

    # Lopper wrapper functions, to avoid everyone looking into the SDT to get
    # the FDT and then calling lopper. This way we can change how the device
    # tree is held in memory, and know one is the wiser.
    def node_abspath( self, tgt_node ):
        return Lopper.node_abspath( self.FDT, tgt_node )

    def node_find_by_name( self, node_name, starting_node = 0 ):
        return Lopper.node_find_by_name( self.FDT, node_name, starting_node )

    def node_properties_as_dict( self, node, verbose=0 ):
        return Lopper.node_properties_as_dict( self.FDT, node, verbose )

    # A thin wrapper + consistent logging and error handling around FDT's
    # node delete
    def node_remove( self, target_node_offset ):
        target_node_name = self.FDT.get_name( target_node_offset )

        if self.verbose > 1:
            print( "[NOTE]: deleting node: %s" % target_node_name )

        self.FDT.del_node( target_node_offset, True )

    def load_assists(self):
        if self.assists:
            sw = libfdt.Fdt.create_empty_tree( 2048 )
            sw.setprop_str( 0, 'compatible', 'system-device-tree-v1' )
            sw.setprop_u32( 0, 'priority', 1)
            offset = sw.add_subnode( 0, 'lops' )

            assist_count = 0
            for a in set(self.assists):
                lop_name = "lop_{}".format( assist_count )
                offset = sw.add_subnode( offset, lop_name )
                sw.setprop_str( offset, 'compatible', 'system-device-tree-v1,lop,load')
                sw.setprop_str( offset, 'load', a )
                lop = Lop( 'commandline' )
                lop.dts = ""
                lop.dtb = ""
                lop.fdt = sw

                if self.verbose > 1:
                    print( "[INFO]: generated load lop for assist %s" % a )

                assist_count = assist_count + 1

            self.lops.insert( 0, lop )

    def domain_spec(self, tgt_domain):
        # This is called from the command line. We need to generate a lop
        # device tree with:
        #
        # lop_0 {
        #     compatible = "system-device-tree-v1,lop,assist-v1";
        #     node = "/chosen/openamp_r5";
        #     id = "openamp,domain-v1";
        # };
        # and then inject it into self.lops to run first

        sw = libfdt.Fdt.create_empty_tree( 2048 )
        sw.setprop_str( 0, 'compatible', 'system-device-tree-v1' )
        offset = sw.add_subnode( 0, 'lops' )
        offset = sw.add_subnode( offset, 'lop_0' )
        sw.setprop_str( offset, 'compatible', 'system-device-tree-v1,lop,assist-v1')
        sw.setprop_str( offset, 'node', '/chosen/openamp_r5' )
        sw.setprop_str( offset, 'id', 'openamp,domain-v1' )
        lop = Lop( 'commandline' )
        lop.dts = ""
        lop.dtb = ""
        lop.fdt = sw

        self.lops.insert( 0, lop )

    # we use the name, rather than the offset, since the offset can change if
    # something is deleted from the tree. But we need to use the full path so
    # we can find it later.
    def node_ref_inc( self, node_name ):
        if self.verbose > 1:
            print( "[INFO]: tracking access to node %s" % node_name )
        if node_name in self.node_access:
            self.node_access[node_name] += 1
        else:
            self.node_access[node_name] = 1

    # get the refcount for a node.
    # node_name is the full path to a node
    def node_ref( self, node_name ):
        if node_name in self.node_access:
            return self.node_access[node_name]
        return -1

    # wrapper
    def node_find( self, node_prefix ):
        return Lopper.node_find( self.FDT, node_prefix )

    def node_type( self, node_offset, verbose=0 ):
        return Lopper.node_type( self.FDT, node_offset, verbose )

    # argument: node number, and an id string
    def find_module_compatible_func( self, cb_node, cb_id ):
        cb_func = []
        if self.modules:
            for m in self.modules:
                cb_f = m.is_compat( cb_node, cb_id )
                if cb_f:
                    cb_func.append( cb_f )
                # we could double check that the function exists with this call:
                #    func = getattr( m, cbname )
                # but for now, we don't
        else:
            print( "[WARNING]: no modules loaded, no compat search is possible" )

        return cb_func

    def perform_lops(self):
        # was --target passed on the command line ?
        if target_domain:
            self.domain_spec(target_domain)

        # force verbose output if --dryrun was passed
        if self.dryrun:
            self.verbose = 2

        if self.verbose:
            print( "[NOTE]: \'%d\' lopper operation input(s) available" % len(self.lops))

        lops_runqueue = {}
        for pri in range(1,10):
            lops_runqueue[pri] = []

        # iterate the lops, look for priority. If we find those, we'll run then first
        for x in self.lops:
            if not x.fdt:
                lops_fdt = libfdt.Fdt(open(x.dtb, mode='rb').read())
                x.fdt = lops_fdt
            else:
                lops_fdt = x.fdt

            lops_file_priority = Lopper.prop_get( lops_fdt, 0, "priority" )
            if not lops_file_priority:
                lops_file_priority = 5

            lops_runqueue[lops_file_priority].append(x)

        if self.verbose > 2:
            print( "[DBG+]: lops runqueue: %s" % lops_runqueue )

        # iterate over the lops (by lop-file priority)
        for pri in range(1,10):
            for x in lops_runqueue[pri]:
                if not x.fdt:
                    lops_fdt = libfdt.Fdt(open(x.dtb, mode='rb').read())
                else:
                    lops_fdt = x.fdt

                # Get all the nodes with a lop property
                lops_nodes = Lopper.nodes_with_property( lops_fdt, "compatible", "system-device-tree-v1,lop.*", "/lops" )
                for n in lops_nodes:
                    prop = lops_fdt.getprop( n, "compatible" )
                    val = Lopper.prop_get( lops_fdt, n, "compatible" )
                    node_name = lops_fdt.get_name( n )

                    if self.verbose:
                        print( "[INFO]: ------> processing lop: %s" % val )
                    if self.verbose > 2:
                        print( "[DBG+]: prop: %s val: %s" % (prop.name, val ))
                        print( "[DBG+]: node name: %s" % node_name )

                    # TODO: need a better way to search for the possible lop types, i.e. a dict
                    if re.search( ".*,output$", val ):
                        output_file_name = Lopper.prop_get( lops_fdt, n, 'outfile' )
                        if not output_file_name:
                            print( "[ERROR]: cannot get output file name from lop" )
                            sys.exit(1)

                        if self.verbose > 1:
                            print( "[DBG+]: outfile is: %s" % output_file_name )

                        output_nodes = Lopper.prop_get( lops_fdt, n, 'nodes', LopperFmt.COMPOUND, LopperFmt.STRING )

                        if self.verbose > 1:
                            print( "[DBG+]: output selected are: %s" % output_nodes )

                        # TODO: allow regexes for nodes
                        if "*" in output_nodes:
                            ff = libfdt.Fdt(self.FDT.as_bytearray())
                        else:
                            # Note: we may want to switch this around, and copy the old tree and
                            #       delete nodes. This will be important if we run into some
                            #       strangely formatted ones that we can't copy.
                            ff = libfdt.Fdt.create_empty_tree( self.FDT.totalsize() )
                            for o_node in output_nodes:
                                # TODO: this really should be using node_find() and we should make sure the
                                #       output 'lop' has full paths.
                                node_to_copy = Lopper.node_find_by_name( self.FDT, o_node, 0 )
                                node_to_copy_path = Lopper.node_abspath( self.FDT, node_to_copy )
                                new_node = Lopper.node_copy_from_path( self.FDT, node_to_copy_path, ff, node_to_copy_path, self.verbose )
                                if not new_node:
                                    print( "[ERROR]: unable to copy node: %s" % node_to_copy_path, )

                        if not self.dryrun:
                            Lopper.write_fdt( ff, output_file_name, True, verbose )
                        else:
                            print( "[NOTE]: dryrun detected, not writing output file %s" % output_file_name )

                    if re.search( ".*,assist-v1$", val ):
                        # also note: this assist may change from being called as part of the
                        # tranform loop, to something that is instead called by walking the
                        # entire device tree, looking for matching nodes and making assists at
                        # that moment.
                        cb_tgt_node_name = Lopper.prop_get( lops_fdt, n, 'node' )
                        if not cb_tgt_node_name:
                            print( "[ERROR]: cannot find target node for the assist" )
                            sys.exit(1)

                        cb = Lopper.prop_get( lops_fdt, n, 'assist' )
                        cb_id = Lopper.prop_get( lops_fdt, n, 'id' )
                        cb_node = Lopper.node_find( self.FDT, cb_tgt_node_name )
                        if cb_node < 0:
                            print( "[ERROR]: cannot find assist target node in tree" )
                            sys.exit(1)
                        if self.verbose:
                            print( "[INFO]: assist lop detected" )
                            if cb:
                                print( "        cb: %s" % cb )
                            print( "        id: %s" % cb_id )

                        cb_funcs = self.find_module_compatible_func( cb_node, cb_id )
                        if cb_funcs:
                            for cb_func in cb_funcs:
                                try:
                                    if not cb_func( cb_node, self, self.verbose ):
                                        print( "[WARNING]: the assist returned false, check for errors ..." )
                                except Exception as e:
                                    print( "[WARNING]: assist %s failed" % cb_func )
                                    exc_type, exc_obj, exc_tb = sys.exc_info()
                                    fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
                                    print(exc_type, fname, exc_tb.tb_lineno)
                                    # exit if warnings are treated as errors
                                    if self.werror:
                                        sys.exit(1)
                        else:
                            print( "[INFO]: no compatible assist found, skipping" )

                    if re.search( ".*,lop,load$", val ):
                        if self.verbose:
                            print( "--------------- [INFO]: node %s is a load module lop" % node_name )
                        try:
                            prop = lops_fdt.getprop( n, 'load' ).as_str()
                        except:
                            prop = ""

                        if prop:
                            if self.verbose:
                                print( "[INFO]: loading module %s" % prop )
                            mod_file = Path( prop )
                            mod_file_wo_ext = mod_file.with_suffix('')
                            try:
                                mod_file_abs = mod_file.resolve()
                            except FileNotFoundError:
                                # check the path from which lopper is running
                                mod_file = Path( lopper_directory + "/" + mod_file.name )
                                try:
                                    mod_file_abs = mod_file.resolve()
                                except FileNotFoundError:
                                    # And finally, try a "assists" subdirectory underneath where
                                    # lopper is running
                                    # TODO: we could read an environment variable to find alternate
                                    #       locations to search for modules.
                                    mod_file = Path( lopper_directory + "/assists/" + mod_file.name )
                                    try:
                                        mod_file_abs = mod_file.resolve()
                                    except FileNotFoundError:
                                        print( "[ERROR]: module file %s not found" % prop )
                                        sys.exit(1)

                            imported_module = __import__(str(mod_file_wo_ext))
                            self.modules.append( imported_module )

                    if re.search( ".*,lop,add$", val ):
                        if self.verbose:
                            print( "[INFO]: node add lop found" )

                        src_node_name = Lopper.prop_get( lops_fdt, n, "node_src" )
                        if not src_node_name:
                            print( "[ERROR]: node add detected, but no node name found" )
                            sys.exit(1)

                        lops_node_path = Lopper.node_abspath( lops_fdt, n )
                        src_node_path = lops_node_path + "/" + src_node_name

                        dest_node_path = Lopper.prop_get( lops_fdt, n, "node_dest" )
                        if not dest_node_path:
                            dest_node_path = "/" + src_node_name

                        if self.verbose:
                            print( "[INFO]: node name: %s node path: %s" % (src_node_path, dest_node_path) )

                        if not Lopper.node_copy_from_path( lops_fdt, src_node_path, self.FDT, dest_node_path, self.verbose ):
                            print( "[ERROR]: unable to copy node: %s" % src_node_name )
                            sys.exit(1)

                    if re.search( ".*,lop,modify$", val ):
                        if self.verbose:
                            print( "[INFO]: node %s is a compatible property modify lop" % node_name )
                        try:
                            prop = lops_fdt.getprop( n, 'modify' ).as_str()
                        except:
                            prop = ""

                        if prop:
                            if self.verbose:
                                print( "[INFO]: modify property found: %s" % prop )

                            # format is: "path":"property":"replacement"
                            #    - modify to "nothing", is a remove operation
                            #    - modify with no property is node operation (rename or remove)
                            modify_expr = prop.split(":")
                            if self.verbose:
                                print( "[INFO]: modify path: %s" % modify_expr[0] )
                                print( "        modify prop: %s" % modify_expr[1] )
                                print( "        modify repl: %s" % modify_expr[2] )

                            if modify_expr[1]:
                                # property operation
                                if not modify_expr[2]:
                                    if self.verbose:
                                        print( "[INFO]: property remove operation detected: %s %s" % (modify_expr[0], modify_expr[1]))
                                    # TODO; make a special case of the property_modify_below
                                    self.property_remove( modify_expr[0], modify_expr[1], True )
                                else:
                                    if self.verbose:
                                        print( "[INFO]: property modify operation detected" )

                                    if Lopper.node_prop_check( self.FDT, modify_expr[0], modify_expr[1] ):
                                        self.property_modify( modify_expr[0], modify_expr[1], modify_expr[2], False )
                                    else:
                                        self.property_modify( modify_expr[0], modify_expr[1], modify_expr[2], False, True )
                            else:
                                # node operation
                                # in case /<name>/ was passed as the new name, we need to drop them
                                # since they aren't valid in set_name()
                                if modify_expr[2]:
                                    modify_expr[2] = modify_expr[2].replace( '/', '' )
                                    try:
                                        tgt_node = Lopper.node_find( self.FDT, modify_expr[0] )
                                        if tgt_node > 0:
                                            if self.verbose:
                                                print("[INFO]: renaming %s to %s" % (modify_expr[0], modify_expr[2]))
                                            self.FDT.set_name( tgt_node, modify_expr[2] )
                                    except:
                                        print( "[ERROR]:cannot rename node: %s %s" %(modify_expr[0], modify_expr[2]))
                                else:
                                    if self.verbose:
                                        print( "[INFO]: node delete: %s" % modify_expr[0] )

                                    node_to_remove = Lopper.node_find( self.FDT, modify_expr[0] )
                                    if node_to_remove > 0:
                                        self.node_remove( node_to_remove )
                                    else:
                                        print( "[ERROR]: cannot find node to remove: %s" % modify_expr[0] )
                                        sys.exit(1)

    # note; this operates on a node and all child nodes, unless you set recursive to False
    def property_remove( self, node_prefix = "/", propname = "", recursive = True ):
        node = Lopper.node_find( self.FDT, node_prefix )
        node_list = []
        depth = 0
        while depth >= 0:
            prop_list = []
            poffset = self.FDT.first_property_offset(node, QUIET_NOTFOUND)
            while poffset > 0:
                # if we delete the only property of a node, all calls to the FDT
                # will throw an except. So if we get an exception, we set our poffset
                # to zero to escape the loop.
                try:
                    prop = self.FDT.get_property_by_offset(poffset)
                except:
                    poffset = 0
                    continue

                prop_list.append(prop.name)
                poffset = self.FDT.next_property_offset(poffset, QUIET_NOTFOUND)

            if propname in prop_list:
                # node is an integer offset, propname is a string
                if self.verbose:
                    print( "[INFO]: removing property %s from %s" % (propname, self.FDT.get_name(node)) )

                self.FDT.delprop(node, propname)

            if recursive:
                node, depth = self.FDT.next_node(node, depth, (libfdt.BADOFFSET,))
            else:
                depth = -1

    # note; this operates on a node and all child nodes, unless you set recursive to False
    def property_modify( self, node_prefix = "/", propname = "", propval = "", recursive = True, add_if_missing = False ):
        node = Lopper.node_find( self.FDT, node_prefix )
        node_list = []
        depth = 0
        while depth >= 0:
            prop_list = []
            poffset = self.FDT.first_property_offset(node, QUIET_NOTFOUND)
            while poffset > 0:
                # if we delete the only property of a node, all calls to the FDT
                # will throw an except. So if we get an exception, we set our poffset
                # to zero to escape the loop.
                try:
                    prop = self.FDT.get_property_by_offset(poffset)
                except:
                    poffset = 0
                    continue

                # print( "propname: %s" % prop.name )
                prop_list.append(prop.name)
                poffset = self.FDT.next_property_offset(poffset, QUIET_NOTFOUND)

            if propname in prop_list:
                # node is an integer offset, propname is a string
                if self.verbose:
                    print( "[INFO]: changing property %s to %s" % (propname, propval ))

                Lopper.prop_set( self.FDT, node, propname, propval )
            else:
                if add_if_missing:
                    try:
                        Lopper.prop_set( self.FDT, node, propname, propval )
                    except:
                        self.FDT.resize( self.FDT.totalsize() + 1024 )
                        Lopper.prop_set( self.FDT, node, propname, propval )


            if recursive:
                node, depth = self.FDT.next_node(node, depth, (libfdt.BADOFFSET,))
            else:
                depth = -1

    def property_set( self, node_number, prop_name, prop_val, ftype=LopperFmt.SIMPLE ):
        return Lopper.prop_set( self.FDT, node_number, prop_name, prop_val, ftype )

    # Note: this is no longer called. possibly delete
    def property_find( self, propname, remove = False ):
        node_list = []
        node = 0
        depth = 0
        while depth >= 0:
            # todo: node_list isn't currently used .. but will be eventually
            node_list.append([depth, self.FDT.get_name(node)])

            prop_list = []
            poffset = self.FDT.first_property_offset(node, QUIET_NOTFOUND)
            while poffset > 0:
                #print( "poffset: %s" % poffset )
                # if we delete the only property of a node, all calls to the FDT
                # will throw an except. So if we get an exception, we set our poffset
                # to zero to escape the loop.
                try:
                    prop = self.FDT.get_property_by_offset(poffset)
                except:
                    poffset = 0
                    continue

                #print( "propname: %s" % prop.name )
                prop_list.append(prop.name)
                poffset = self.FDT.next_property_offset(poffset, QUIET_NOTFOUND)

                if propname in prop_list:
                    # node is an integer offset, propname is a string
                    if self.verbose:
                        print( "[INFO]: removing property %s from %s" % (propname, self.FDT.get_name(node)) )

                    if remove:
                        self.FDT.delprop(node, propname)

            node, depth = self.FDT.next_node(node, depth, (libfdt.BADOFFSET,))

    def property_get( self, node_number, property_name, ftype=LopperFmt.SIMPLE, encode=LopperFmt.DEC ):
        # just a wrapper routine ..
        return Lopper.prop_get( self.FDT, node_number, property_name, ftype, encode )

    def inaccessible_nodes( self, propname ):
        node_list = []
        node = 0
        depth = 0
        while depth >= 0:
            prop_list = []
            poffset = self.FDT.first_property_offset( node, QUIET_NOTFOUND )
            while poffset > 0:
                prop = self.FDT.get_property_by_offset( poffset )
                val = Lopper.property_value_decode( prop, poffset )

                if propname == prop.name:
                    if propname == "inaccessible":
                        # - the labels in the nodes are converted to <0x03>
                        # - and there is an associated node with phandle = <0x03>
                        # - so we need to take the phandle, and find the node that has that value

                        tgt_node = self.FDT.node_offset_by_phandle( val )
                        if not tgt_node in node_list:
                            node_list.append(tgt_node)
                            #node_list.append([depth, self.FDT.get_name(node)])

                        if self.verbose:
                            print( "[NOTE]: %s has inaccessible specified for %s" %
                                       (self.FDT.get_name(node), self.FDT.get_name(tgt_node)))

                poffset = self.FDT.next_property_offset(poffset, QUIET_NOTFOUND)

            node, depth = self.FDT.next_node(node, depth, (libfdt.BADOFFSET,))

        if self.verbose:
            if node_list:
                print( "[INFO]: removing inaccessible nodes: %s" % node_list )

                for tgt_node in node_list:
                    # TODO: catch the errors here, since the target node may not have
                    #       had a proper label, so the phandle may not be valid
                    self.node_remove( tgt_node )

class Lop:
    def __init__(self, lop_file):
        self.dts = lop_file
        self.dtb = ""
        self.fdt = ""

def usage():
    prog = os.path.basename(sys.argv[0])
    print('Usage: %s [OPTION] <system device tree> [<output file>]...' % prog)
    print('  -v, --verbose       enable verbose/debug processing (specify more than once for more verbosity)')
    print('  -t, --target        indicate the starting domain for processing (i.e. chosen node or domain label)' )
    print('    , --dryrun        run all processing, but don\'t write any output files' )
    print('  -d, --dump          dump a dtb as dts source' )
    print('  -i, --input         process supplied input device tree description')
    print('  -a, --assist        load specified python assist (for node or output processing)' )
    print('  -o, --output        output file')
    print('  -f, --force         force overwrite output file(s)')
    print('    , --werror        treat warnings as errors' )
    print('  -S, --save-temps    don\'t remove temporary files' )
    print('  -h, --help          display this help and exit')
    print('')

##
##
## Thoughts:
##    - could take stdin as a lop tree (not very usful)
##    - add an option to take a sdt and convert it to yaml (aka pretty print)
##    - may need to take -I for the search paths when we run dtc as part of the processing
##
##

def main():
    global inputfiles
    global output
    global output_file
    global sdt
    global sdt_file
    global verbose
    global force
    global dump_dtb
    global target_domain
    global dryrun
    global assists
    global werror
    global save_temps

    sdt = None
    verbose = 0
    output = ""
    inputfiles = []
    force = False
    dump_dtb = False
    dryrun = False
    target_domain = ""
    assists = []
    werror = False
    save_temps = False
    try:
        opts, args = getopt.getopt(sys.argv[1:], "t:dfvdhi:o:a:S", [ "save-temps", "werror","target=", "dump", "force","verbose","help","input=","output=","dryrun","assist="])
    except getopt.GetoptError as err:
        print('%s' % str(err))
        usage()
        sys.exit(2)

    if opts == [] and args == []:
        usage()
        sys.exit(1)

    for o, a in opts:
        if o in ('-v', "--verbose"):
            verbose = verbose + 1
        elif o in ('-d', "--dump"):
            dump_dtb = True
        elif o in ('-f', "--force"):
            force = True
        elif o in ('-h', '--help'):
            usage()
            sys.exit(0)
        elif o in ('-i', '--input'):
            inputfiles.append(a)
        elif o in ('-a', '--assist'):
            assists.append(a)
        elif o in ('-t', '--target'):
            target_domain = a
        elif o in ('-o', '--output'):
            output = a
        elif o in ('--dryrun'):
            dryrun=True
        elif o in ('--werror'):
            werror=True
        elif o in ('-S', '--save-temps' ):
            save_temps=True
        else:
            assert False, "unhandled option"

    # any args should be <system device tree> <output file>
    for idx, item in enumerate(args):
        # validate that the system device tree file exists
        if idx == 0:
            sdt = item
            sdt_file = Path(sdt)
            try:
                my_abs_path = sdt_file.resolve()
            except FileNotFoundError:
                # doesn't exist
                print( "Error: system device tree %s does not exist" % sdt )
                sys.exit(1)

        # the last input is the output file. It can't already exist, unless
        # --force was passed
        if idx == 1:
            if output:
                print( "Error: output was already provided via -o\n")
                usage()
                sys.exit(1)
            else:
                output = item
                output_file = Path(output)
                if output_file.exists():
                    if not force:
                        print( "Error: output file %s exists, and -f was not passed" % output )
                        sys.exit(1)

    if not sdt:
        print( "[ERROR]: no system device tree was supplied\n" )
        usage()
        sys.exit(1)

    # check that the input files (passed via -i) exist
    for i in inputfiles:
        inf = Path(i)
        if not inf.exists():
            print( "Error: input file %s does not exist" % i )
            sys.exit(1)

        valid_ifile_types = [ ".dtsi", ".dtb", ".dts" ]
        itype = Lopper.input_file_type(i)
        if not itype in valid_ifile_types:
            print( "[ERROR]: unrecognized input file type passed" )
            sys.exit(1)


if __name__ == "__main__":

    # Main processes the command line, and sets some global variables we
    # use below
    main()

    if dump_dtb:
        Lopper.dtb_dts_export( sdt, verbose )
        sys.exit(0)

    device_tree = SystemDeviceTree( sdt )

    atexit.register(at_exit_cleanup)

    # set some flags before we process the tree.
    device_tree.dryrun = dryrun
    device_tree.verbose = verbose
    device_tree.werror = werror
    device_tree.output_file = output
    device_tree.cleanup_flag = True
    device_tree.save_temps = save_temps

    device_tree.setup( sdt, inputfiles, "", assists, force )
    device_tree.perform_lops()

    if not dryrun:
        Lopper.write_sdt( device_tree, output, True, device_tree.verbose )
    else:
        print( "[INFO]: --dryrun was passed, output file %s not written" % output )

    device_tree.cleanup()