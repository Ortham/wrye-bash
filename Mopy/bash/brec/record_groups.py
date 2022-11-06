# -*- coding: utf-8 -*-
#
# GPL License and Copyright Notice ============================================
#  This file is part of Wrye Bash.
#
#  Wrye Bash is free software: you can redistribute it and/or
#  modify it under the terms of the GNU General Public License
#  as published by the Free Software Foundation, either version 3
#  of the License, or (at your option) any later version.
#
#  Wrye Bash is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with Wrye Bash.  If not, see <https://www.gnu.org/licenses/>.
#
#  Wrye Bash copyright (C) 2005-2009 Wrye, 2010-2022 Wrye Bash Team
#  https://github.com/wrye-bash
#
# =============================================================================
"""Houses classes for reading, manipulating and writing groups of records."""

__author__ = 'Utumno'

# Python imports
from collections import deque, defaultdict
from functools import wraps
from itertools import chain
from operator import itemgetter, attrgetter

# Wrye Bash imports
from . import utils_constants, MelRecord
from .mod_io import GrupHeader, RecordHeader, TopGrupHeader, \
    ExteriorGrupHeader, ChildrenGrupHeader, FastModReader, unpack_header
from .utils_constants import fid_key, DUMMY_FID, group_types
from ..bolt import pack_int, structs_cache, attrgetter_cache, sig_to_str, \
    dict_sort
from ..exception import AbstractError, ModError, ModFidMismatchError

class _AMobBase:
    """Group of records and/or subgroups."""
    def __init__(self, loadFactory, ins):
        self.changed = False
        self.loadFactory = loadFactory
        # due to subclasses sometimes setting ins to None
        self.inName = getattr(self, 'inName', None) or (ins and ins.inName)

    def _load_err(self, msg): ##: add ins and print more info
        raise ModError(self.inName, msg)

    def setChanged(self,value=True):
        """Sets changed attribute to value. [Default = True.]"""
        self.changed = value

    ##: This should be dropped later once we ensure we correctly call
    # setChanged() on all non-BP uses of ModFile where we modify records and
    # make the BP call setChanged() during its record trimming phase on all
    # keepIds - hard right now due to having to trace ModFile.load() calls.
    def set_records_changed(self):
        """Mark all records in this record group as changed."""
        if type(self) != MobBase: # ugh
            for r in self.iter_records():
                r.setChanged()

    def updateMasters(self, masterset_add):
        """Updates set of master names according to masters actually used."""
        for record in self.iter_records():
            record.updateMasters(masterset_add)

    # Abstract methods --------------------------------------------------------
    def getSize(self):
        """Returns size (including size of any group headers)."""
        raise AbstractError

    def getNumRecords(self,includeGroups=True):
        """Returns number of records, including self (if plusSelf), unless
        there's no subrecords, in which case, it returns 0."""
        raise AbstractError

    def dump(self, out):
        """Dumps record header and data into output file stream."""
        raise AbstractError

    def iter_present_records(self, include_ignored=False, rec_key='fid',
                             __attrgetters=attrgetter_cache):
        """Filters iter_records, returning only records that have not set
        the deleted flag and/or the ignore flag if include_ignored is False."""
        key_get = __attrgetters[rec_key]
        return ((key_get(r), r) for r in self.iter_records() if not
                r.flags1.deleted and (include_ignored or not r.flags1.ignored))

    def get_all_signatures(self):
        """Returns a set of all signatures actually contained in this block."""
        raise AbstractError('get_all_signatures not implemented')

    def iter_records(self):
        """Flattens the structure of this record block into a linear sequence
        of records. Works as an iterator for memory reasons."""
        raise AbstractError('iter_records not implemented')

    def keepRecords(self, p_keep_ids):
        """Keeps records with fid in set p_keep_ids. Discards the rest."""
        raise AbstractError('keepRecords not implemented')

    def _load_rec_group(self, ins, endPos):
        """Loads data from input stream. Called by __init__()."""
        raise AbstractError('_load_rec_group not implemented')

    ##: params here are not the prettiest
    def merge_records(self, block, loadSet, mergeIds, iiSkipMerge, doFilter):
        """Merges records from the specified block into this block and performs
        merge filtering if doFilter is True.

        :param block: The block to merge records from.
        :param loadSet: The set of currently loaded plugins.
        :param mergeIds: A set into which the fids of all records that will be
            merged by this operation will be added.
        :param iiSkipMerge: If True, skip merging and only perform merge
            filtering. Used by IIM mode.
        :param doFilter: If True, perform merge filtering."""
        raise AbstractError(f'{self}: merge_records not implemented')

    def updateRecords(self, srcBlock, mergeIds):
        """Update the contents of records inside the BP to ones from a
        source plugin. We never add new records into the BP here. That's
        only done by importers (using setRecord) or by merging files (via
        merge_records)."""
        raise AbstractError('updateRecords not implemented')

class MobBase(_AMobBase):
    """Group of records and/or subgroups. This basic implementation does not
    support unpacking, but can report its number of records and be dumped."""

    def __init__(self, grup_head, loadFactory, ins=None, do_unpack=False):
        super().__init__(loadFactory, ins)
        self.header = grup_head
        self.size = grup_head.size
        self.label, self.groupType, self.stamp = (
            grup_head.label, grup_head.groupType, grup_head.stamp)
        # binary blob of the whole record group minus its GRUP header ##: rename
        self.data = None
        self.numRecords = -1
        if ins:
            #--Read, but don't analyze.
            if not do_unpack:
                self.data = ins.read(grup_head.blob_size(),
                                     type(self).__name__)
            #--Analyze ins.
            elif ins is not None:
                self._load_rec_group(ins, ins.tell() + grup_head.blob_size())
            #--Discard raw data?
            if do_unpack:
                self.data = None
                self.setChanged()

    def getSize(self):
        """Returns size (including size of any group headers)."""
        if self.changed: raise AbstractError
        return self.size

    def getNumRecords(self,includeGroups=True):
        """Returns number of records, including self (if plusSelf), unless
        there's no subrecords, in which case, it returns 0."""
        if self.changed:
            raise AbstractError
        elif self.numRecords > -1: #--Cached value.
            return self.numRecords
        elif not self.data: #--No data >> no records, not even self.
            self.numRecords = 0
            return self.numRecords
        else:
            numSubRecords = 0
            num_groups = 1 # the top level grup itself - not included in data
            with FastModReader(self.inName, self.data) as ins:
                ins_tell = ins.tell
                ins_size = ins.size
                while ins_tell() != ins_size:
                    header = unpack_header(ins)
                    if header.recType != b'GRUP':
                        # FMR.seek doesn't have *debug_str arg so use blob_size
                        ins.seek(header.blob_size(), 1) # instead of skip_blob
                        numSubRecords += 1
                    else: num_groups += 1
            self.numRecords = numSubRecords + includeGroups * num_groups
            return self.numRecords

    def dump(self,out):
        """Dumps record header and data into output file stream."""
        if self.changed:
            raise AbstractError
        if self.numRecords == -1:
            self.getNumRecords()
        if self.numRecords > 0:
            self.header.size = self.size
            out.write(self.header.pack_head())
            out.write(self.data)

#------------------------------------------------------------------------------
class MobObjects(MobBase):
    """Represents a group consisting of the record types specified in
    _accepted_sigs."""
    _bad_form = None # yak
    _accepted_sigs = {b'OVERRIDE'} # overriden via properties must be static
    _grup_header_type: type[GrupHeader] | None = None

    def __init__(self, header, loadFactory, ins=None, do_unpack=False):
        self.id_records = {}
        if ins and all(loadFactory.sig_to_type[r] is None for r in
                       self._accepted_sigs):
            header.skip_blob(ins)
            self.inName = ins.inName
            ins = None # block _load_rec_group
        super().__init__(header, loadFactory, ins, do_unpack)

    def _load_rec_group(self, ins, endPos):
        """Loads data from input stream. Called by load()."""
        insAtEnd = ins.atEnd
        errLabel = f'{self}'
        while not insAtEnd(endPos,errLabel):
            #--Get record info and handle it
            header = unpack_header(ins)
            if header.recType not in self._accepted_sigs:
                self._load_err(f'Unexpected {header!r} record in {errLabel}.')
            grel = self._group_element(header, ins)
            if grel is not None: self.setRecord(grel, _loading=True)
            else: header.skip_blob(ins)
        self.setChanged()

    def _group_element(self, header, ins, do_unpack=True) -> 'MreRecord':
        rec_class = self.loadFactory.sig_to_type[header.recType]
        return None if rec_class is None else rec_class(header, ins,
                                                        do_unpack=do_unpack)

    def _mob_header(self, *args): # FIXME was TESIV only - self.extra??
        return self._grup_header_type(self.getSize(), self.label, self.stamp)

    @classmethod
    def empty_mob(cls, loadFactory, head_label, gt, *args):
        """Get an empty MobObjects collection to use in merge_records etc."""
        mob = cls(cls._grup_header_type(0, head_label, gt, *args), loadFactory)
        mob.setChanged()
        return mob

    def __bool__(self): return bool(self.id_records)

    def getActiveRecords(self):
        """Returns non-ignored records - XXX what about isKeyedByEid?"""
        return [(r.fid, r) for r in self.id_records.values() if
                not r.flags1.ignored]

    def getNumRecords(self,includeGroups=True):
        """Returns number of records, including self - if empty return 0."""
        num_recs = len(self.id_records)
        if num_recs: num_recs += includeGroups #--Count self
        self.numRecords = num_recs
        return num_recs

    def getSize(self):
        """Returns size (including size of any group headers)."""
        if not self.changed:
            return self.size
        else:
            if not self.id_records: return 0
            hsize = RecordHeader.rec_header_size
            recs_size = sum((hsize + r.getSize()) for r in self.id_records.values())
            return hsize + recs_size # add hsize for the GRUP header

    def dump(self,out):
        """Dumps group header and then records."""
        if not self.changed:
            out.write(self.header.pack_head())
            out.write(self.data)
        else:
            if not self.id_records: return
            out.write(self._mob_header().pack_head())
            self._sort_group()
            for record in self.id_records.values():
                record.dump(out)

    def _sort_group(self):
        """Sorts records by FormID - now eid order matters too for
        isKeyedByEid records."""
        self.id_records = dict(dict_sort(self.id_records))

    def updateMasters(self, masterset_add):
        """Updates set of master names according to masters actually used."""
        for record in self.iter_records():
            record.updateMasters(masterset_add)

    def getRecord(self, rec_rid):
        """Gets record with corresponding id.
        If record doesn't exist, returns None."""
        return self.id_records.get(rec_rid, None)

    def setRecord(self, record, do_copy=True, _loading=False):
        """Adds record to self.id_records."""
        el_key = record.group_key()
        if _loading and el_key in self.id_records:
            self._load_err(f'Duplicate {el_key} record in {self}')
        self.id_records[el_key] = record if _loading or not do_copy else \
            record.getTypeCopy()

    def keepRecords(self, p_keep_ids):
        """Keeps records with fid in set p_keep_ids. Discards the rest."""
        self.id_records = {rec_key: record for rec_key, record in
                           self.id_records.items() if rec_key in p_keep_ids}
        self.setChanged()

    def updateRecords(self, srcBlock, mergeIds):
        merge_ids_discard = mergeIds.discard
        copy_to_self = self.setRecord
        dest_rec_fids = self.id_records
        for rid, record in srcBlock.getActiveRecords():
            if rid in dest_rec_fids:
                copy_to_self(record)
                merge_ids_discard(rid)

    def merge_records(self, block, loadSet, mergeIds, iiSkipMerge, doFilter):
        # YUCK, drop these local imports!
        from ..mod_files import MasterSet
        filtered = {}
        loadSetIsSuperset = loadSet.issuperset
        mergeIdsAdd = mergeIds.add
        copy_to_self = self.setRecord
        for rid, src_rec in block.getActiveRecords():
            if rid == self._bad_form: continue
            #--Include this record?
            if doFilter:
                # If we're Filter-tagged, perform merge filtering. Then, check
                # if the record has any FormIDs with masters that are on disk
                # left. If it does not, skip the whole record (because all of
                # its contents have been merge-filtered out).
                src_rec.mergeFilter(loadSet)
                masterset = MasterSet()
                src_rec.updateMasters(masterset.add)
                if not loadSetIsSuperset(masterset):
                    continue
            # We're either not Filter-tagged or we want to keep this record
            filtered[rkey := src_rec.group_key()] = src_rec
            # If we're IIM-tagged and this is not one of the IIM-approved
            # record types, skip merging
            if not iiSkipMerge:
                # We're past all hurdles - stick a copy of this record into
                # ourselves and mark it as merged
                mergeIdsAdd(rkey)
                copy_to_self(src_rec)
        # Apply any merge filtering we've done above to the record block in
        # question. That way, patchers won't see the records that have been
        # filtered out here.
        block.id_records = filtered

    def iter_records(self):
        return self.id_records.values()

    def __repr__(self):
        return f'<{self}: {len(self.id_records)} record(s)>'

    def __str__(self): return f'{group_types[self.header.groupType]} GRUP'

class _ChildrenGrup(MobObjects):
    """Represents a children group."""
    _grup_header_type = ChildrenGrupHeader
    _children_grup_type = -1

    def __init__(self, header, loadFactory, ins=None, do_unpack=False,
                 master_rec=None):
        if master_rec and (groupFid := header.label) != master_rec.fid:
            self._load_err(f'Children subgroup ({groupFid}) does '
                           f'not match parent {master_rec!r}.')
        super().__init__(header, loadFactory, ins, do_unpack)

    def get_all_signatures(self):
        return {r._rec_sig for r in self.id_records.values()}

    def _mob_header(self):
        return self._grup_header_type(self.getSize(), self.header.label,
            self._children_grup_type, self.header.stamp)

_EOF = -1 # endPos is at the end of file - used for non-headed groups
class _Nested(_AMobBase):
    """A nested grup of records with some optional records[ in front]."""
    # sig -> attr - these records should appear at most once except if required
    _extra_records: dict[bytes, str] = {}
    # we need to know the top type this group belongs to for _load_rec_group
    _top_type = None
    _mob_objects_type: dict[int, type[_ChildrenGrup]] = {}
    _mob_objects: dict[int, _ChildrenGrup]

    def __init__(self, loadFactory, ins=None, **kwargs):
        for att in self._extra_records.values():
            setattr(self, att, kwargs.get(att, None))
        self._mob_objects = {}
        super().__init__(loadFactory, ins)
        if ins:
            ins.rewind()
            self._load_rec_group(ins, _EOF) # we don't know the size in advance
        self._set_mob_objects()

    def _set_mob_objects(self, head_label=None):
        for gt, mob_type in self._mob_objects_type.items():
            if not self._mob_objects.get(gt):
                self._mob_objects[gt] = mob_type.empty_mob(self.loadFactory,
                    head_label or DUMMY_FID, gt)

    def getSize(self):
        return sum(chg.getSize() for chg in chain(self._stray_recs().values(),
                                                  self._mob_objects.values()))

    def getNumRecords(self,includeGroups=True):
        nrecs = sum(1 for __ in self.iter_records())
        if includeGroups and nrecs: # no recs => no top or nested groups
            nrecs += sum(1 for m in self._mob_objects if m)
        return nrecs

    def get_all_signatures(self):
        return {i._rec_sig for i in self.iter_records()}

    def iter_records(self):
        return chain(iter(self._stray_recs().values()),
                     *(v.iter_records() for v in self._mob_objects.values()))

    def merge_records(self, src_block, loadSet, mergeIds, iiSkipMerge, doFilter):
        from ..mod_files import MasterSet # YUCK
        mergeIdsAdd = mergeIds.add
        loadSetIsSuperset = loadSet.issuperset
        for single_attr in self._extra_records.values():
            # Grab the version that we're trying to merge, and check if there's
            # even one present
            src_rec = getattr(src_block, single_attr)
            if src_rec and not src_rec.flags1.ignored:
                # If we're Filter-tagged, perform merge filtering first
                if doFilter:
                    src_rec.mergeFilter(loadSet)
                    masterset = MasterSet()
                    src_rec.updateMasters(masterset.add)
                    if not loadSetIsSuperset(masterset):
                        # Filtered out, discard this record and skip to next
                        setattr(src_block, single_attr, None)
                        continue
                # In IIM, skip all merging (duh)
                if not iiSkipMerge:
                    # We're past all hurdles - stick a copy of this record into
                    # ourselves and mark it as merged
                    mergeIdsAdd(src_rec.fid)
                    setattr(self, single_attr, src_rec.getTypeCopy())
        for gt, block_mob in src_block._mob_objects.items():
            self._mob_objects[gt].merge_records(block_mob, loadSet, mergeIds,
                                                iiSkipMerge, doFilter)

    def updateRecords(self, srcBlock, mergeIds):
        for chg in self._mob_objects.values():
            chg.updateRecords(srcBlock, mergeIds)
        for att in srcBlock._extra_records.values():
            if (rec := getattr(srcBlock, att)) and not rec.flags1.ignored:
                setattr(self, att, rec.getTypeCopy())
                mergeIds.discard(rec.fid)

    def keepRecords(self, p_keep_ids):
        for chg in self._mob_objects.values():
            chg.keepRecords(p_keep_ids)
        for att in self._extra_records.values():
            if (rec := getattr(self, att)) and rec.group_key() not in p_keep_ids:
                setattr(self, att, None)

    def _load_rec_group(self, ins, endPos, master_rec=None):
        """Loads data from input stream. Called by load()."""
        insAtEnd = ins.atEnd
        errLabel = f'{self}'
        subgroups_loaded = set()
        while not insAtEnd(endPos, errLabel):
            #--Get record info and handle it
            header = unpack_header(ins)
            if (head_sig := header.recType) in self._extra_records:
                rec_name = self._extra_records[head_sig]
                if getattr(self, rec_name) is not None:
                    if head_sig == self._top_type: # next record
                        ins.rewind()
                        break
                    self._load_err(f'Duplicate {rec_name}')
                grel = self._group_element(header, ins)
                if grel: setattr(self, rec_name, grel)
                else: header.skip_blob(ins)
            elif head_sig == b'GRUP':
                if header.is_top_group_header: # next Top Grup
                    ins.rewind()
                    break
                if (gt := header.groupType) in subgroups_loaded:
                    self._load_err(f'Duplicate subgroup type {gt} in {self}')
                subgroups_loaded.add(gt)
                try:
                    self._mob_objects[gt] = self._mob_objects_type[gt](
                        header, self.loadFactory, ins, do_unpack=True,
                        master_rec=master_rec)
                except KeyError:
                    raise ModError(ins.inName, f'Sub {gt} in {self}')
            else:
                self._load_err(f'Unexpected {sig_to_str(head_sig)} record in '
                               f'{self} group.')
        self.setChanged()

    def _load_mobs(self, gt, header, ins, master_rec):
        try:
            self._mob_objects[gt] = self._mob_objects_type[gt](header,
                self.loadFactory, ins, do_unpack=True, master_rec=master_rec)
        except KeyError:
            self._load_err(f'Sub {gt} in {self}')

    def _group_element(self, header, ins, do_unpack=True) -> 'MreRecord':
        rec_class = self.loadFactory.sig_to_type[header.recType]
        return None if rec_class is None else rec_class(header, ins,
                                                        do_unpack=do_unpack)

    def dump(self, out):
        for r in self._stray_recs().values():
            r.dump(out)
        for m in self._mob_objects.values():
            m.dump(out)

    def _stray_recs(self):
        return {att: rec for att in self._extra_records.values() if
                (rec := getattr(self, att, None))}

def _process_rec(sig, after=True, target=None):
    """Hack to do some processing before or after calling super method omitting
    the processed signature (or self._top_grup). Must only be used *once*
    per method override chain (else RecursionError). If we do the processing
    before, and it returns falsy, super won't be called."""
    def _exclude_rec(meth):
        @wraps(meth)
        def _call_super(self, *args, **kwargs):
            if not after:
                if not meth(self, *args, **kwargs): # don't call super
                    return
            target_ = self if target is None else args[target]
            old = getattr(target_, '_extra_records')
            try:
                recs = dict(old)
                del recs[self._top_type if sig is None else sig]
                # we decorate the final method in the inheritance chain, and we
                # want to call the immediate ancestor - get that
                parent_function = [f for t in type(self).__mro__ if
                                   (f := t.__dict__.get(meth.__name__))][1]
                parent_function(self, *args, **kwargs)
            finally:
                setattr(target_, '_extra_records', old)
            # Now execute the logic for the excluded record
            if after: meth(self, *args, **kwargs)
        return _call_super
    return _exclude_rec

class _ComplexRec(_Nested):
    """Itself a collection of records - a required record, and a MobObjects."""

    @property
    def master_record(self):
        """The master CELL, WRLD or DIAL record."""
        return getattr(self, self._extra_records[self._top_type])

    @master_record.setter
    def master_record(self, rec):
        setattr(self, self._extra_records[self._top_type], rec)

    def group_key(self): return self.master_record.group_key()

    def _set_mob_objects(self, head_label=None): ## fixme override in cell
        super()._set_mob_objects(self.master_record.group_key())

    def _load_rec_group(self, ins, endPos, master_rec=None):
        """Loads data from input stream. Called by load()."""
        if ins.atEnd(endPos, f'{self}'): return # empty top level group?
        header = unpack_header(ins)
        if self._top_type != header.recType: # first record is required
            raise ModError(ins.inName, f'Missing '
                f'{self._extra_records[self._top_type]} from {self}')
        recClass = self.loadFactory.sig_to_type[header.recType]
        self.master_record = recClass(header, ins, do_unpack=True)
        super()._load_rec_group(ins, endPos, self.master_record)

    @_process_rec(None)
    def keepRecords(self, p_keep_ids):
        if mr_rec := self.master_record:
            # Get rid of the master record and look for children
            self.master_record = None
            any_children_kept = any(self.iter_records())
            if any_children_kept or mr_rec.group_key() in p_keep_ids:
                # Either we want to keep the master record or at least one of
                # its children, so restore the master record and keep it if it
                # isn't already kept. Otherwise, just don't undo the master
                # record deletion from before
                p_keep_ids.add(mr_rec.group_key())
                self.master_record = mr_rec

    @_process_rec(None, after=False, target=0)
    def updateRecords(self, srcBlock, mergeIds):
        src_dial = srcBlock.master_record
        # Copy the latest version of the master record over. We can safely mark
        # it as not merged because keepRecords above ensures that we never
        # discard a master record when it still has children
        if not src_dial.flags1.ignored:
            self.master_record = src_dial.getTypeCopy()
            mergeIds.discard(src_dial.fid)
            return True # call super

    @_process_rec(None, after=False)
    def merge_records(self, src_block, loadSet, mergeIds, iiSkipMerge, doFilter):
        from ..mod_files import MasterSet # YUCK
        mergeIdsAdd = mergeIds.add
        loadSetIsSuperset = loadSet.issuperset
        # First, check the main DIAL record
        src_rec = src_block.master_record
        if not src_rec.flags1.ignored:
            # If we're Filter-tagged, perform merge filtering first
            if doFilter:
                src_rec.mergeFilter(loadSet)
                masterset = MasterSet()
                src_rec.updateMasters(masterset.add)
                if not loadSetIsSuperset(masterset):
                    # Filtered out, discard this DIAL record (and, by
                    # extension, all its INFO children)
                    self.master_record = None # will drop us from MobDials
                    return
            # In IIM, we can't just return here since we also need to filter
            # the children that came with this complex record
            if not iiSkipMerge:
                # We're past all hurdles - mark the record as merged, and stick
                # a copy into ourselves
                mergeIdsAdd(src_rec.fid)
                self.master_record = src_rec.getTypeCopy()
            # Now we're ready to filter and merge the children
            return True

    def __str__(self):
        return f'{next(iter(self._extra_records))} Block'

class TopGrup(MobObjects):
    """Represents a top level group with simple records. I.e. all top groups
    except CELL, WRLD and DIAL."""
    _grup_header_type = TopGrupHeader

    def __init__(self, header, loadFactory, ins=None, do_unpack=False):
        self._accepted_sigs = {header.label} # needed in _load_rec_group
        super().__init__(header, loadFactory, ins, do_unpack)

    def get_all_signatures(self):
        return self._accepted_sigs if self else set()

    def __str__(self):
        return f'{sig_to_str(self.header.label)} {super().__str__()}'

class TopComplexGrup(TopGrup):
    """CELL, WRLD and DIAL."""
    _top_rec_class: type[_ComplexRec] = None

    def get_all_signatures(self):
        return {*chain(
            *(r.get_all_signatures() for r in self.id_records.values()))}

    def _group_element(self, header, ins, do_unpack=True) -> _ComplexRec:
        return self._top_rec_class(self.loadFactory, ins)

    def getActiveRecords(self): ### FIXME rid
        """Returns non-ignored records - XXX what about isKeyedByEid?"""
        raise AbstractError
        return [(r.fid, r) for r in self.id_records.values() if
                not r.flags1.ignored]

    def getNumRecords(self,includeGroups=True):
        """Returns number of records, including self - if empty return 0."""
        if not self: return 0
        num_recs = sum(r.getNumRecords(includeGroups) for r in
                       self.id_records.values()) + includeGroups #--Count self
        self.numRecords = num_recs
        return num_recs

    def getSize(self):
        """Returns size (including size of any group headers)."""
        if not self.changed:
            return self.size
        else:
            if not self.id_records: return 0
            recs_size = sum((r.getSize()) for r in self.id_records.values())
            # add hsize for the GRUP header
            return RecordHeader.rec_header_size + recs_size

    def iter_records(self):
        return chain(*(d.iter_records() for d in self.id_records.values()))

    def setRecord(self, block, do_copy=True, _loading=False):
        """Adds the specified complex record to self, overriding an existing
        one with the same FormID or creating a new complex record block."""
        if isinstance(block, MelRecord):
            # it's the block's master record (needed for _merge_records)
            if not _loading and do_copy:
                block = block.getTypeCopy()
            master_rec = block
            block = self._group_element(None, ins=None, **{ # the master record
                sig_to_str(master_rec._rec_sig).lower(): master_rec})
            block.setChanged()
        grel_key = block.group_key()
        if grel_key in self.id_records:
            if _loading:
                self._load_err(f'Duplicate {grel_key} record in {self}')
            self.id_records[grel_key].master_record = block.master_record
        else:
            self.id_records[grel_key] = block
        return self.id_records[grel_key] # complex records: return this back

    def keepRecords(self, p_keep_ids):
        for complex_rec in self.id_records.values():
            complex_rec.keepRecords(p_keep_ids)
        # loop above may set complex_rec.master_record to None
        self.id_records = {k: d for k, d in self.id_records.items() if
                           d.master_record is not None}
        self.setChanged()

    def updateRecords(self, srcBlock, mergeIds):
        lookup_dial = self.id_records.get
        for dfid, src_dial in srcBlock.id_records.items():
            # Check if we have a corresponding DIAL record in the destination
            dest_dial = lookup_dial(dfid)
            if dest_dial:
                dest_dial.updateRecords(src_dial, mergeIds)

    def merge_records(self, block, loadSet, mergeIds, iiSkipMerge, doFilter):
        from ..mod_files import MasterSet # YUCK
        lookup_dial = self.id_records.get
        filtered_dials = {}
        loadSetIsSuperset = loadSet.issuperset
        for src_fid, src_dialogue in block.id_records.items():
            # Check if we already have a dialogue with that FormID
            dest_dialogue = lookup_dial(src_fid)
            if was_newly_added := not dest_dialogue:
                # We do not, add it and get it - will typeCopy the rec
                dest_dialogue = self.setRecord(src_dialogue.dial)
            # Delegate merging to the (potentially newly added) child dialogue
            dest_dialogue.merge_records(src_dialogue, loadSet, mergeIds,
                iiSkipMerge, doFilter)
            # In IIM, skip all merging - note that we need to remove the child
            # dialogue again if it was newly added in IIM mode.
            if iiSkipMerge:
                if was_newly_added:
                    del self.id_records[dest_dialogue.group_key()]
                continue
            # If we're Filter-tagged, check if the dialogue got filtered out
            if doFilter:
                masterset = MasterSet()
                src_dialogue.updateMasters(masterset.add)
                if not loadSetIsSuperset(masterset):
                    # The child dialogue got filtered out. If it was newly
                    # added, we need to remove it from this block again.
                    # Otherwise, we can just skip forward to the next child.
                    if was_newly_added:
                        del self.id_records[dest_dialogue.group_key()]
                    continue
            # We're either not Filter-tagged or we want to keep this dialogue
            filtered_dials[src_fid] = src_dialogue
        # Apply any merge filtering we've done above to the record block
        block.id_records = filtered_dials

#------------------------------------------------------------------------------
class MobDial(_ComplexRec):
    """A single DIAL with INFO children."""
    _extra_records = {b'DIAL': 'dial'}
    _top_type = b'DIAL'

    class _DialChildren(_ChildrenGrup):
        _accepted_sigs = {b'INFO'}
        _children_grup_type = 7
        _stamp2 = 0

        def _mob_header(self):
            return ChildrenGrupHeader(self.getSize(), self.header.label, 7,
                                      self.header.stamp, self._stamp2)

        def _sort_group(self):
            """Sorts the INFOs of this DIAL record by their (PNAM) Previous
            Info. These do not simply describe a linear list, but a directed
            graph - e.g. you can have edges B->A and C->A, which would leave
            both C->B->A and B->C->A as valid orders. To decide in such
            cases, we stick with whatever the previous order was.

            Note: We assume the PNAM graph is acyclic - cyclic graphs are
            errors in plugins anyways, so the behavior of PBash when
            encountering such errors is undefined."""
            # First gather a list of all 'orphans', i.e. INFOs that have no
            # PNAM. We'll start with these and insert non-orphans into the
            # list at the right spot based on their PNAM
            sorted_infos = []
            remaining_infos = deque()
            for r in self.iter_records():
                if not r.prev_info:
                    sorted_infos.append(r)
                else:
                    remaining_infos.append(r)
            visited_fids = set()
            while remaining_infos:
                # Pop from the right to maintain the original sort order when
                # inserting multiple INFOs with the same PNAM
                curr_info = remaining_infos.pop()
                wanted_prev_fid = curr_info.prev_info
                # Look if a record matching the PNAM has already been inserted
                for i, prev_candidate in enumerate(sorted_infos):
                    if prev_candidate.fid == wanted_prev_fid:
                        # It has, so just insert our INFO after it
                        sorted_infos.insert(i + 1, curr_info)
                        break
                else:
                    # Not in the sorted INFOs, check for a cycle/unknown record
                    ##: This isn't wholly correct - really, we'd have to
                    # check for cycles and missing FIDs here, then behave as
                    # follows:
                    #  - missing PNAM FID: Exactly like right now, append to
                    #    sorted
                    #  - cycle: raise error/deprint
                    #  - otherwise: re-appendleft the FID again, keep going
                    #    until we've added its PNAM
                    if curr_info.fid in visited_fids:
                        # Either the PNAM points to a record that's not in our
                        # file (which is fine and happens all the time),
                        # or this INFO is in a cycle, or the PNAM points to
                        # a non-existent record.
                        # To handle this situation, we simply append it to the
                        # end of our sorted INFOs.
                        # We don't warn here because trying to differentiate
                        # the valid and common case from the two error cases
                        # would be too slow. xEdit can do this much better.
                        sorted_infos.append(curr_info)
                    else:
                        # We'll have to revisit this INFO later when its
                        # PNAM may have been added, so move it to the end (
                        # == left side) of the queue
                        visited_fids.add(curr_info.fid)
                        remaining_infos.appendleft(curr_info)
            self.id_records = {rec.fid: rec for rec in sorted_infos}
    _mob_objects_type = {_DialChildren._children_grup_type: _DialChildren}

    def dump(self, out):
        # Update TIFC if needed (i.e. Skyrim+)
        if hasattr(mr := self.master_record, 'info_count'):
            mr.info_count = len(self._mob_objects[7].id_records)
        super().dump(out)

    def __repr__(self):
        nrecs = len(self._mob_objects[7].id_records)
        return f'<DIAL ({self.dial!r}): {nrecs} INFO record(s)>'

    def set_stamp(self, val):
        self._mob_objects[7].header.stamp = val

class MobDials(TopComplexGrup):
    """DIAL top block of mod file."""
    _top_rec_class = MobDial

    def getSize(self):
        """Patch _mob_objects (INFO) headers are created with 0 stamp - I
        repeated here what the old code did, but we should check."""
        for dialogue in self.id_records.values():
            dialogue.set_stamp(self.header.stamp)
        return super().getSize()

#------------------------------------------------------------------------------
class MobCell(MobBase):
    """Represents cell block structure -- including the cell and all
    subrecords."""
    __slots__ = ('cell', 'persistent_refs', 'distant_refs', 'temp_refs',
                 'land', 'pgrd')

    def __init__(self, header, loadFactory, cell, ins=None, do_unpack=False):
        self.cell = cell
        self.persistent_refs = []
        self.distant_refs = []
        self.temp_refs = []
        self.land = None
        self.pgrd = None
        super(MobCell, self).__init__(header, loadFactory, ins, do_unpack)

    def _load_rec_group(self, ins, endPos):
        """Loads data from input stream. Called by load()."""
        cellType_class = self.loadFactory.getCellTypeClass()
        insAtEnd = ins.atEnd
        cellGet = cellType_class.get
        persistentAppend = self.persistent_refs.append
        tempAppend = self.temp_refs.append
        distantAppend = self.distant_refs.append
        subgroupLoaded = set()
        groupType = None # guaranteed to compare False to any of them
        while not insAtEnd(endPos, u'Cell Block'):
            header = unpack_header(ins)
            _rsig = header.recType
            recClass = cellGet(_rsig)
            if _rsig == b'GRUP':
                groupType = header.groupType
                if groupType not in (8, 9, 10):
                    raise ModError(self.inName,
                                   f'Unexpected subgroup {groupType:d} in '
                                   f'cell children group.')
                if groupType in subgroupLoaded:
                    raise ModError(self.inName,
                                   f'Extra subgroup {groupType:d} in cell '
                                   f'children group.')
                subgroupLoaded.add(groupType)
            elif _rsig not in cellType_class:
                raise ModError(self.inName,
                               f'Unexpected {sig_to_str(_rsig)} record in '
                               f'cell children group.')
            elif not recClass:
                header.skip_blob(ins)
            elif _rsig in (b'REFR',b'ACHR',b'ACRE'):
                record = recClass(header, ins, do_unpack=True)
                if   groupType ==  8: persistentAppend(record)
                elif groupType ==  9: tempAppend(record)
                elif groupType == 10: distantAppend(record)
            elif _rsig == b'LAND':
                self.land = recClass(header, ins, do_unpack=True)
            elif _rsig == b'PGRD':
                self.pgrd = recClass(header, ins, do_unpack=True)
        self.setChanged()

    def getSize(self):
        """Returns size (including size of any group headers)."""
        return RecordHeader.rec_header_size + self.cell.getSize() + \
               self._get_children_arrays()[0]

    def _get_children_arrays(self):
        """Returns size of all children, including the group header.  This
        does not include the cell itself. Returns the arrays themselves ready
        for dump complete with their size and group_type."""
        hsize = RecordHeader.rec_header_size
        children_arrays = []
        children = self._cell_children()
        for refs, group_type in zip(children, (8, 9, 10)):
            if refs:
                refs_size = sum(hsize + x.getSize() for x in refs) + hsize
                children_arrays.append([refs_size, group_type, refs])
        children_size = sum(t[0] for t in children_arrays)
        if children_size: # include the GRUP (type 6) header size
            children_size += hsize
        return children_size, children_arrays

    def _cell_children(self):
        return [self.persistent_refs,
                # The order is LAND -> PGRD -> temporary references
                [*(x for x in (self.land, self.pgrd) if x), *self.temp_refs],
                self.distant_refs]

    def getNumRecords(self,includeGroups=True):
        """Returns number of records, including self and all children."""
        count = sum((len(children) + includeGroups) for children in
                     self._cell_children() if children)
        if count:
            # CELL GRUP only exists if the CELL has at least one child
            count += includeGroups
        return count + 1 # CELL record, always present

    def dump(self,out):
        """Dumps group header and then records."""
        self.cell.getSize()
        self.cell.dump(out)
        children_size, refs_size = self._get_children_arrays()
        if not children_size: return
        self._sort_group()
        self._write_children_group(out, children_size, 6)
        # The order is persistent -> temporary -> distant
        for gsize, gtype, refs in refs_size:
            self._write_children_group(out, gsize, gtype, *refs)

    def _write_children_group(self, out, group_size, group_type, *elements):
        out.write(ChildrenGrupHeader(group_size, self.cell.fid, group_type,
            self.stamp).pack_head()) # FIXME was TESIV only - self.extra??
        for element in elements:
            element.dump(out)

    #--Record filtering ----------------------------------
    def get_all_signatures(self):
        return {group_el._rec_sig for group_el in self.iter_records()}

    def _sort_group(self):
        """Sort temporary/persistent/distant references by FormID."""
        self.persistent_refs.sort(key=fid_key)
        self.temp_refs.sort(key=fid_key)
        self.distant_refs.sort(key=fid_key)

    def updateMasters(self, masterset_add):
        """Updates set of master names according to masters actually used."""
        self.cell.updateMasters(masterset_add)
        for record in chain(self.persistent_refs, self.distant_refs,
                            self.temp_refs):
            record.updateMasters(masterset_add)
        if self.land:
            self.land.updateMasters(masterset_add)
        if self.pgrd:
            self.pgrd.updateMasters(masterset_add)

    def updateRecords(self, srcBlock, mergeIds, __attrget=attrgetter(
        u'cell', u'pgrd', u'land', u'persistent_refs', u'temp_refs',
        u'distant_refs')):
        """Updates any records in 'self' that exist in 'srcBlock'."""
        mergeDiscard = mergeIds.discard
        self_src_attrs = list(zip(__attrget(self), __attrget(srcBlock)))
        for attr, (myRecord, record) in zip((u'cell', u'pgrd', u'land'),
                                             self_src_attrs):
            if myRecord and record:
                if not record.flags1.ignored:
                    setattr(self, attr, record.getTypeCopy())
                    mergeDiscard(record.fid)
        for attr, (self_rec_list, src_rec_list) in zip(
                (u'persistent_refs', u'temp_refs', u'distant_refs'),
                self_src_attrs[3:]):
            fids = {record.fid: i for i, record in enumerate(self_rec_list)}
            for record in src_rec_list:
                src_fid = record.fid
                if not record.flags1.ignored and src_fid in fids:
                    self_rec_list[fids[src_fid]] = record.getTypeCopy()
                    mergeDiscard(src_fid)

    def iter_records(self):
        if self.cell:
            yield self.cell
        yield from chain(*self._cell_children())

    def keepRecords(self, p_keep_ids):
        """Keeps records with fid in set p_keep_ids. Discards the rest."""
        if self.pgrd and self.pgrd.fid not in p_keep_ids:
            self.pgrd = None
        if self.land and self.land.fid not in p_keep_ids:
            self.land = None
        self.temp_refs = [x for x in self.temp_refs if x.fid in p_keep_ids]
        self.persistent_refs = [x for x in self.persistent_refs
                                if x.fid in p_keep_ids]
        self.distant_refs = [x for x in self.distant_refs
                             if x.fid in p_keep_ids]
        if (self.pgrd or self.land or self.persistent_refs or self.temp_refs or
                self.distant_refs):
            p_keep_ids.add(self.cell.fid)
        self.setChanged()

    def merge_records(self, block, loadSet, mergeIds, iiSkipMerge, doFilter):
        from ..mod_files import MasterSet # YUCK
        mergeIdsAdd = mergeIds.add
        loadSetIsSuperset = loadSet.issuperset
        for single_attr in (u'cell', u'pgrd', u'land'):
            # Grab the version that we're trying to merge, and check if there's
            # even one present
            src_rec = getattr(block, single_attr)
            if src_rec and not src_rec.flags1.ignored:
                # If we're Filter-tagged, perform merge filtering first
                if doFilter:
                    src_rec.mergeFilter(loadSet)
                    masterset = MasterSet()
                    src_rec.updateMasters(masterset.add)
                    if not loadSetIsSuperset(masterset):
                        # Filtered out, discard this record and skip to next
                        setattr(block, single_attr, None)
                        continue
                # In IIM, skip all merging (duh)
                if not iiSkipMerge:
                    # We're past all hurdles - stick a copy of this record into
                    # ourselves and mark it as merged
                    mergeIdsAdd(src_rec.fid)
                    setattr(self, single_attr, src_rec.getTypeCopy())
        for list_attr in (u'temp_refs', u'persistent_refs', u'distant_refs'):
            filtered_list = []
            filtered_append = filtered_list.append
            # Build a mapping from fids in the current list to the index at
            # which they're stored ##: cache? see also updateRecords above
            dest_list = getattr(self, list_attr)
            append_to_dest = dest_list.append
            id_fids = {record.fid: i for i, record
                       in enumerate(dest_list)}
            for src_rec in getattr(block, list_attr):
                if src_rec.flags1.ignored: continue
                # If we're Filter-tagged, perform merge filtering first
                if doFilter:
                    src_rec.mergeFilter(loadSet)
                    masterset = MasterSet()
                    src_rec.updateMasters(masterset.add)
                    if not loadSetIsSuperset(masterset):
                        continue
                # We're either not Filter-tagged or we want to keep this record
                filtered_append(src_rec)
                # In IIM, skip all merging (duh)
                if iiSkipMerge: continue
                # We're past all hurdles - stick a copy of this record into
                # ourselves and mark it as merged
                src_fid = src_rec.fid
                rec_copy = src_rec.getTypeCopy()
                mergeIdsAdd(src_fid)
                if rec_copy.fid in id_fids:
                    dest_list[id_fids[src_fid]] = rec_copy
                else:
                    append_to_dest(rec_copy)
            # Apply any merge filtering we've done here
            setattr(block, list_attr, filtered_list)

    def __repr__(self):
        pgrd_ = f'PGRD: {self.pgrd!r}' if self.pgrd else 'no PGRD'
        land_ = f'LAND: {self.land!r}' if self.land else 'no LAND'
        return f'<CELL ({self.cell!r}): {len(self.persistent_refs)} ' \
               f'persistent record(s), {len(self.distant_refs)} distant ' \
               f'record(s), {len(self.temp_refs)} temporary record(s), ' \
               f'{land_}, {pgrd_}>'

#------------------------------------------------------------------------------
class MobCells(MobBase):
    """A block containing cells. Subclassed by MobWorld and MobICells.

    Note that "blocks" here only roughly match the file block structure.

    "Bsb" is a tuple of the file (block,subblock) labels. For interior
    cells, bsbs are tuples of two numbers, while for exterior cells, bsb labels
    are tuples of grid tuples."""

    def __init__(self, header, loadFactory, ins=None, do_unpack=False):
        #--Each cellBlock is a cell and its related records.
        self.id_cellBlock: dict[utils_constants.FormId, MobCell] = {}
        super(MobCells, self).__init__(header, loadFactory, ins, do_unpack)

    def setCell(self, cell, ins=None, children_head=None, loading=False,
                skip_delta=False, unpack_block=None):
        """Adds a copy of the specified CELL to this CELLs block."""
        if not loading:
            cell = cell.getTypeCopy()
        cfid = cell.fid
        if cfid in self.id_cellBlock:
            if loading: raise ModError(self.inName,
                                       f'Duplicate {cfid} record in {self}')
            self.id_cellBlock[cfid].cell = cell
        else:
            if children_head is None: # no infos! ins should be None
                children_head = ChildrenGrupHeader(0, DUMMY_FID, 6, self.stamp)
            cellBlock = MobCell(children_head, self.loadFactory, cell,
                                ins=unpack_block and ins, do_unpack=unpack_block)
            cellBlock.setChanged()
            if not unpack_block and skip_delta:
                children_head.skip_blob(ins)
            self.id_cellBlock[cfid] = cellBlock

    def _load_group6(self, header, cell, ins, unpackCellBlocks):
        groupFid: utils_constants.FormId = header.label
        if cell:
            if groupFid != cell.fid:
                raise ModError(self.inName, f'Cell subgroup ({groupFid}) '
                                            f'does not match CELL {cell!r}.')
            self.setCell(cell, ins=ins, children_head=header, loading=True,
                         skip_delta=True, unpack_block=unpackCellBlocks)
            return None
        raise ModError(self.inName,
                       f'Extra subgroup {header.groupType:d} in {self}')

    def remove_cell(self, cell):
        """Removes the specified cell from this block. The exact cell object
        must be present, otherwise a ValueError is raised."""
        del self.id_cellBlock[cell.fid]

    def getBsbSizes(self): ##: This is the _sort_group for MobCells
        """Returns the total size of the block, but also returns a
        dictionary containing the sizes of the individual block,subblocks."""
        # First sort by the CELL FormID, then by the block they belong to
        bsbCellBlocks = [(cb.cell.getBsb(), cb) for _cfid, cb in
                         dict_sort(self.id_cellBlock)]
        bsbCellBlocks.sort(key=itemgetter(0))
        # Calculate total size and create block/subblock sizes dict to update
        # block GRUP headers
        totalSize = hsize = RecordHeader.rec_header_size
        bsb_size = defaultdict(lambda: hsize)
        for bsb,cellBlock in bsbCellBlocks:
            cellBlockSize = cellBlock.getSize()
            totalSize += cellBlockSize
            bsb0 = (bsb[0],None) #--Block group
            if bsb not in bsb_size:
                bsb_size[bsb0] += hsize
            bsb_size[bsb] += cellBlockSize
            bsb_size[bsb0] += cellBlockSize
        totalSize += hsize * len(bsb_size)
        return totalSize,bsb_size,bsbCellBlocks

    def dumpBlocks(self,out,bsbCellBlocks,bsb_size,blockGroupType,
                   subBlockGroupType):
        """Dumps the cell blocks and their block and sub-block groups to
        out."""
        curBlock = None
        curSubblock = None
        stamp = self.stamp
        outWrite = out.write
        if (blockGroupType, subBlockGroupType) == (2, 3):
            grup_htype = GrupHeader
        elif (blockGroupType, subBlockGroupType) == (4, 5):
            grup_htype = ExteriorGrupHeader
        else:
            raise ValueError(f'{(blockGroupType, subBlockGroupType)} is '
                             f'invalid')
        for bsb,cellBlock in bsbCellBlocks:
            (block,subblock) = bsb
            bsb0 = (block,None)
            if block != curBlock:
                curBlock,curSubblock = bsb0
                outWrite(grup_htype(bsb_size[bsb0], block, blockGroupType,
                                    stamp).pack_head())
            if subblock != curSubblock:
                curSubblock = subblock
                outWrite(grup_htype(bsb_size[bsb], subblock, subBlockGroupType,
                                    stamp).pack_head())
            cellBlock.dump(out)

    def getNumRecords(self,includeGroups=True):
        """Returns number of records, including self and all children."""
        count = sum(
            x.getNumRecords(includeGroups) for x in self.id_cellBlock.values())
        if count and includeGroups:
            blocks_bsbs = {x2.cell.getBsb() for x2 in self.id_cellBlock.values()}
            # 1 GRUP header for every cellBlock and one for each separate (?) subblock
            count += 1 + len(blocks_bsbs) + len({x1[0] for x1 in blocks_bsbs})
        return count

    #--Fid manipulation, record filtering ----------------------------------
    def get_all_signatures(self):
        return set(chain.from_iterable(c.get_all_signatures()
                                       for c in self.id_cellBlock.values()))

    def iter_records(self):
        return chain.from_iterable(
            c.iter_records() for c in self.id_cellBlock.values())

    def keepRecords(self, p_keep_ids):
        """Keeps records with fid in set p_keep_ids. Discards the rest."""
        #--Note: this call will add the cell to p_keep_ids if any of its
        # related records are kept.
        for cellBlock in self.id_cellBlock.values():
            cellBlock.keepRecords(p_keep_ids)
        self.id_cellBlock = {k: v for k, v in self.id_cellBlock.items() if
                             k in p_keep_ids}
        self.setChanged()

    def merge_records(self, block, loadSet, mergeIds, iiSkipMerge, doFilter):
        from ..mod_files import MasterSet # YUCK
        lookup_cell_block = self.id_cellBlock.get
        filtered_cell_blocks = {}
        loadSetIsSuperset = loadSet.issuperset
        for src_fid, src_cell_block in block.id_cellBlock.items():
            was_newly_added = False
            # Check if we already have a cell with that FormID
            dest_cell_block = lookup_cell_block(src_fid)
            if not dest_cell_block:
                # We do not, add it and then look up again
                self.setCell(src_cell_block.cell)
                dest_cell_block = lookup_cell_block(src_fid)
                was_newly_added = True
            # Delegate merging to the (potentially newly added) child cell
            dest_cell_block.merge_records(src_cell_block, loadSet,
                mergeIds, iiSkipMerge, doFilter)
            # In IIM, skip all merging - note that we need to remove the child
            # cell again if it was newly added in IIM mode.
            if iiSkipMerge:
                if was_newly_added:
                    self.remove_cell(dest_cell_block.cell)
                continue
            # If we're Filter-tagged, check if the child cell got filtered out
            if doFilter:
                masterset = MasterSet()
                src_cell_block.updateMasters(masterset.add)
                if not loadSetIsSuperset(masterset):
                    # The child cell got filtered out. If it was newly added,
                    # we need to remove it from this block again. Otherwise, we
                    # can just skip forward to the next child cell.
                    if was_newly_added:
                        self.remove_cell(dest_cell_block.cell)
                    continue
            # We're either not Filter-tagged or we want to keep this cell
            filtered_cell_blocks[src_fid] = src_cell_block
        # Apply any merge filtering we've done above to the record block
        block.id_cellBlock = filtered_cell_blocks

    def updateRecords(self, srcBlock, mergeIds):
        """Updates any records in 'self' that exist in 'srcBlock'."""
        id_Get = self.id_cellBlock.get
        for src_block_cfid, srcCellBlock in srcBlock.id_cellBlock.items():
            cellBlock = id_Get(src_block_cfid)
            if cellBlock:
                cellBlock.updateRecords(srcCellBlock, mergeIds)

    def updateMasters(self, masterset_add):
        """Updates set of master names according to masters actually used."""
        for cellBlock in self.id_cellBlock.values():
            cellBlock.updateMasters(masterset_add)

    def __repr__(self):
        return f'<CELL GRUP: {len(self.id_cellBlock)} record(s)>'

#------------------------------------------------------------------------------
class MobICells(MobCells):
    """Tes4 top block for interior cell records."""

    def _load_rec_group(self, ins, endPos):
        """Loads data from input stream. Called by load()."""
        expType = self.label
        recClass = self.loadFactory.sig_to_type[expType]
        insSeek = ins.seek
        if not recClass: insSeek(endPos) # skip the whole group
        cell = None
        endBlockPos = endSubblockPos = 0
        unpackCellBlocks = self.loadFactory.getUnpackCellBlocks(b'CELL')
        insAtEnd = ins.atEnd
        insTell = ins.tell
        selfLoadFactory = self.loadFactory
        while not insAtEnd(endPos, f'{sig_to_str(expType)} Top Block'):
            header = unpack_header(ins)
            _rsig = header.recType
            if _rsig == expType:
                if cell:
                    # If we already have a cell lying around, finish it off
                    self.setCell(cell, children_head=None, loading=True)
                cell = recClass(header, ins, do_unpack=True)
                if (pos := insTell()) > endBlockPos or pos > endSubblockPos:
                    raise ModError(self.inName,
                                   f'Interior cell <{cell.fid:X}> {cell.eid} '
                                   f'outside of block or subblock.')
            elif _rsig == b'GRUP':
                groupType = header.groupType
                if groupType == 2: # Block number
                    endBlockPos = insTell() + header.blob_size()
                elif groupType == 3: # Sub-block number
                    endSubblockPos = insTell() + header.blob_size()
                elif groupType == 6: # Cell Children
                    self._load_group6(header, cell, ins, unpackCellBlocks)
                    cell = None
                else:
                    self._load_err(
                        f'Unexpected subgroup {groupType:d} in CELL group.')
            else:
                self._load_err(f'Unexpected {sig_to_str(_rsig)} record in '
                               f'{sig_to_str(expType)} group.')
        if cell:
            # We have a CELL without children left over, finish it
            self.setCell(cell, children_head=None, loading=True)
        self.setChanged()

    def dump(self,out):
        """Dumps group header and then records."""
        if not self.changed:
            out.write(self.header.pack_head())
            out.write(self.data)
        elif self.id_cellBlock:
            (totalSize, bsb_size, blocks) = self.getBsbSizes()
            self.header.size = totalSize
            out.write(self.header.pack_head())
            self.dumpBlocks(out,blocks,bsb_size,2,3)

#------------------------------------------------------------------------------
class MobWorld(MobCells):

    def __init__(self, header, loadFactory, world, ins=None, do_unpack=False):
        self.world = world
        ##: rename to e.g. persistent_block, this is the cell block that houses
        # all persistent objects in the worldspace
        self.worldCellBlock = None
        self.road = None
        super(MobWorld, self).__init__(header, loadFactory, ins, do_unpack)

    def _load_rec_group(self, ins, endPos, *, __packer=structs_cache['I'].pack,
                        __unpacker=structs_cache['2h'].unpack):
        """Loads data from input stream. Called by load()."""
        cellType_class = self.loadFactory.getCellTypeClass()
        errLabel = u'World Block'
        cell = None
        endBlockPos = endSubblockPos = 0
        unpackCellBlocks = self.loadFactory.getUnpackCellBlocks(b'WRLD')
        insAtEnd = ins.atEnd
        cellGet = cellType_class.get
        insTell = ins.tell
        selfLoadFactory = self.loadFactory
        cells = {}
        while not insAtEnd(endPos,errLabel):
            curPos = insTell()
            if curPos >= endBlockPos:
                endBlockPos = 0
            #--Get record info and handle it
            header = unpack_header(ins)
            _rsig = header.recType
            recClass = cellGet(_rsig)
            if _rsig == b'ROAD':
                if not recClass: header.skip_blob(ins)
                else: self.road = recClass(header, ins, do_unpack=True)
            elif _rsig == b'CELL':
                if cell:
                    # If we already have a cell lying around, finish it off
                    self.setCell(cell, children_head=None, loading=True)
                cell = recClass(header, ins, do_unpack=True)
                cells[cell.fid] = cell
                if endBlockPos and ((pos := insTell()) > endBlockPos or pos >
                              endSubblockPos):
                    raise ModError(self.inName,
                            f'Exterior cell {cell!r} after block or subblock.')
            elif _rsig == b'GRUP':
                groupType = header.groupType
                if groupType == 4: # Exterior Cell Block
                    endBlockPos = insTell() + header.blob_size()
                elif groupType == 5: # Exterior Cell Sub-Block
                    endSubblockPos = insTell() + header.blob_size()
                elif groupType == 6: # Cell Children
                    cell = cells.get(header.label,None)
                    self._load_group6(header, cell, ins, unpackCellBlocks)
                    cell = None
                else:
                    raise ModError(self.inName,
                                   f'Unexpected subgroup {groupType:d} in '
                                   f'world children group.')
            else:
                raise ModError(self.inName,
                               f'Unexpected {sig_to_str(_rsig)} record in'
                               f'world children group.')
        if cell:
            # We have a CELL without children left over, finish it
            self.setCell(cell, children_head=None, loading=True)
        self.setChanged()

    def setCell(self, cell, ins=None, children_head=None, loading=False,
                skip_delta=False, unpack_block=None):
        super().setCell(cell, ins, children_head, loading,
                        skip_delta, unpack_block)
        if loading and cell.flags1.persistent:
            if self.worldCellBlock:
                raise ModError(self.inName,
                               f'Misplaced exterior cell {cell!r}.')
            self.worldCellBlock = self.id_cellBlock.pop(cell.fid)

    def getNumRecords(self,includeGroups=True):
        """Returns number of records, including self and all children."""
        if not self.changed:
            return super(MobCells, self).getNumRecords(includeGroups) ##: TTT?
        count = 1 # self.world, always present
        count += bool(self.road)
        if self.worldCellBlock:
            count += self.worldCellBlock.getNumRecords(includeGroups)
        count += super(MobWorld, self).getNumRecords(includeGroups)
        return count

    def dump(self,out):
        """Dumps group header and then records.  Returns the total size of
        the world block."""
        hsize = RecordHeader.rec_header_size
        worldSize = self.world.getSize() + hsize
        self.world.dump(out)
        if not self.changed:
            out.write(self.header.pack_head())
            out.write(self.data)
            return self.size + worldSize
        elif self.id_cellBlock or self.road or self.worldCellBlock:
            (totalSize, bsb_size, blocks) = self.getBsbSizes()
            if self.road:
                totalSize += self.road.getSize() + hsize
            if self.worldCellBlock:
                totalSize += self.worldCellBlock.getSize()
            self.header.size = totalSize
            self.header.label = self.world.fid
            self.header.groupType = 1
            out.write(self.header.pack_head())
            # The order is ROAD -> persistent CELL -> blocks
            if self.road:
                self.road.dump(out)
            if self.worldCellBlock:
                self.worldCellBlock.dump(out)
            self.dumpBlocks(out,blocks,bsb_size,4,5)
            return totalSize + worldSize
        else:
            return worldSize

    def set_persistent_cell(self, cell):
        """Updates the persistent CELL block to use a copy of the specified
        CELL or creates a new persistent CELL block if one does not already
        exist in this world."""
        cell_copy = cell.getTypeCopy()
        if self.worldCellBlock:
            self.worldCellBlock.cell = cell_copy
        else:
            children_head = ChildrenGrupHeader(0, DUMMY_FID, 6, self.stamp)
            new_pers_block = MobCell(children_head, self.loadFactory, cell_copy)
            new_pers_block.setChanged()
            self.worldCellBlock = new_pers_block

    #--Fid manipulation, record filtering ----------------------------------
    def get_all_signatures(self):
        all_sigs = super(MobWorld, self).get_all_signatures()
        all_sigs.add(self.world._rec_sig)
        if self.road: all_sigs.add(self.road._rec_sig)
        if self.worldCellBlock:
            all_sigs |= self.worldCellBlock.get_all_signatures()
        return all_sigs

    def updateMasters(self, masterset_add):
        """Updates set of master names according to masters actually used."""
        self.world.updateMasters(masterset_add)
        if self.road:
            self.road.updateMasters(masterset_add)
        if self.worldCellBlock:
            self.worldCellBlock.updateMasters(masterset_add)
        super(MobWorld, self).updateMasters(masterset_add)

    def updateRecords(self, srcBlock, mergeIds):
        """Updates any records in 'self' that exist in 'srcBlock'."""
        for attr in (u'world', u'road'):
            myRecord = getattr(self, attr)
            record = getattr(srcBlock, attr)
            if myRecord and record:
                src_rec_fid = record.fid
                ##: This may be wrong, check if ROAD behaves like PGRD/LAND
                if myRecord.fid != src_rec_fid:
                    raise ModFidMismatchError(self.inName, myRecord.rec_str,
                                              myRecord.fid, src_rec_fid)
                if not record.flags1.ignored:
                    record = record.getTypeCopy()
                    setattr(self, attr, record)
                    mergeIds.discard(src_rec_fid)
        if self.worldCellBlock and srcBlock.worldCellBlock:
            self.worldCellBlock.updateRecords(srcBlock.worldCellBlock,
                mergeIds)
        super(MobWorld, self).updateRecords(srcBlock, mergeIds)

    def iter_records(self):
        single_recs = [x for x in (self.world, self.road) if x]
        c_recs = (self.worldCellBlock.iter_records() if self.worldCellBlock
                  else [])
        return chain(single_recs, c_recs, super(MobWorld, self).iter_records())

    def keepRecords(self, p_keep_ids):
        """Keeps records with fid in set p_keep_ids. Discards the rest."""
        if self.road and self.road.fid not in p_keep_ids:
            self.road = None
        if self.worldCellBlock:
            self.worldCellBlock.keepRecords(p_keep_ids)
            if self.worldCellBlock.cell.fid not in p_keep_ids:
                self.worldCellBlock = None
        super(MobWorld, self).keepRecords(p_keep_ids)
        if self.road or self.worldCellBlock or self.id_cellBlock:
            p_keep_ids.add(self.world.fid)

    def merge_records(self, block, loadSet, mergeIds, iiSkipMerge, doFilter):
        from ..mod_files import MasterSet # YUCK
        mergeIdsAdd = mergeIds.add
        loadSetIsSuperset = loadSet.issuperset
        for single_attr in (u'world', u'road'):
            src_rec = getattr(block, single_attr)
            if src_rec and not src_rec.flags1.ignored:
                # If we're Filter-tagged, perform merge filtering first
                if doFilter:
                    src_rec.mergeFilter(loadSet)
                    masterset = MasterSet()
                    src_rec.updateMasters(masterset.add)
                    if not loadSetIsSuperset(masterset):
                        # Filtered out, discard this record and skip to next
                        setattr(block, single_attr, None)
                        continue
                # In IIM, skip all merging (duh)
                if iiSkipMerge: continue
                dest_rec = getattr(self, single_attr)
                ##: This may be wrong, check if ROAD behaves like PGRD/LAND
                if dest_rec and dest_rec.fid != src_rec.fid:
                    raise ModFidMismatchError(self.inName, dest_rec.rec_str,
                                              dest_rec.fid, src_rec.fid)
                # We're past all hurdles - stick a copy of this record into
                # ourselves and mark it as merged
                mergeIdsAdd(src_rec.fid)
                setattr(self, single_attr, src_rec.getTypeCopy())
        if block.worldCellBlock:
            was_newly_added = False
            # If we don't have a world cell block yet, make a new one to merge
            # the source's world cell block into
            if not self.worldCellBlock:
                children_head = ChildrenGrupHeader(0, DUMMY_FID, 6, self.stamp)
                self.worldCellBlock = MobCell(children_head, self.loadFactory,
                    None) # cell will be set in merge_records
                was_newly_added = True
            # Delegate merging to the (potentially newly added) block
            self.worldCellBlock.merge_records(block.worldCellBlock, loadSet,
                mergeIds, iiSkipMerge, doFilter)
            # In IIM, skip all merging - note that we need to remove the world
            # cell block again if it was newly added in IIM mode.
            if iiSkipMerge:
                if was_newly_added:
                    self.worldCellBlock = None
            elif doFilter:
                # If we're Filter-tagged, check if the world cell block got
                # filtered out
                masterset = MasterSet()
                self.worldCellBlock.updateMasters(masterset.add)
                if not loadSetIsSuperset(masterset):
                    # The cell block got filtered out. If it was newly added,
                    # we need to remove it from this block again.
                    if was_newly_added:
                        self.worldCellBlock = None
        super(MobWorld, self).merge_records(block, loadSet, mergeIds,
            iiSkipMerge, doFilter)

    def __repr__(self):
        persistent_cell = f'persistent CELL: {self.worldCellBlock!r}' if \
            self.worldCellBlock else 'no persistent CELL'
        road_ = f'ROAD: {self.road!r}' if self.road else 'no ROAD'
        return f'<WRLD ({self.world!r}): {len(self.id_cellBlock)} ' \
               f'record(s), {persistent_cell}, {road_}>'

#------------------------------------------------------------------------------
class MobWorlds(MobBase):
    """Tes4 top block for world records and related roads and cells. Consists
    of world blocks."""

    def __init__(self, header, loadFactory, ins=None, do_unpack=False):
        self.id_worldBlocks = {}
        self.orphansSkipped = 0
        super(MobWorlds, self).__init__(header, loadFactory, ins, do_unpack)

    def _load_rec_group(self, ins, endPos):
        """Loads data from input stream. Called by load()."""
        expType = self.label
        recWrldClass = self.loadFactory.sig_to_type[expType]
        insSeek = ins.seek
        if not recWrldClass: insSeek(endPos) # skip the whole group
        world = None
        insAtEnd = ins.atEnd
        selfLoadFactory = self.loadFactory
        worlds = {}
        header = None
        while not insAtEnd(endPos, f'{sig_to_str(expType)} Top Block'):
            #--Get record info and handle it
            prev_header = header
            header = unpack_header(ins)
            _rsig = header.recType
            if _rsig == expType:
                # FIXME(inf) The getattr here has to go
                if (prev_header and
                        getattr(prev_header, u'recType', None) == b'WRLD'):
                    # We hit a WRLD directly after another WRLD, so there are
                    # no children to read - just finish this WRLD
                    self.setWorld(world)
                world = recWrldClass(header, ins, do_unpack=True)
                worlds[world.fid] = world
            elif _rsig == b'GRUP':
                groupFid,groupType = header.label,header.groupType
                if groupType != 1:
                    raise ModError(ins.inName,
                                   f'Unexpected subgroup {groupType:d} in '
                                   f'WRLD group.')
                world = worlds.get(groupFid,None)
                if not world:
                    #--Orphaned world records. Skip over.
                    header.skip_blob(ins)
                    self.orphansSkipped += 1
                    continue
                if groupFid != (wfid := world.fid):
                    raise ModError(ins.inName,
                                   f'WRLD subgroup ({hex(groupFid)}) does '
                                   f'not match WRLD {world!r}.')
                worldBlock = MobWorld(header,selfLoadFactory,world,ins,True)
                self.id_worldBlocks[wfid] = worldBlock
                world = None
            else:
                raise ModError(ins.inName,
                               f'Unexpected {sig_to_str(_rsig)} record in'
                               f'{expType} group.')
        if world:
            # We have a last WRLD without children lying around, finish it
            self.setWorld(world)
        self.setChanged()

    def getSize(self):
        """Returns size (including size of any group headers)."""
        return RecordHeader.rec_header_size + sum(
            x.getSize() for x in self.id_worldBlocks.values())

    def dump(self,out):
        """Dumps group header and then records."""
        if not self.changed:
            out.write(self.header.pack_head())
            out.write(self.data)
        else:
            if not self.id_worldBlocks: return
            worldHeaderPos = out.tell()
            header = TopGrupHeader(0, self.label, self.stamp)
            out.write(header.pack_head())
            self._sort_group()
            ##: Why not use getSize here?
            totalSize = RecordHeader.rec_header_size + sum(
                x.dump(out) for x in self.id_worldBlocks.values())
            out.seek(worldHeaderPos + 4)
            pack_int(out, totalSize)
            out.seek(worldHeaderPos + totalSize)

    def getNumRecords(self,includeGroups=True):
        """Returns number of records, including self and all children."""
        count = sum(x.getNumRecords(includeGroups) for x in self.id_worldBlocks.values())
        return count + includeGroups * bool(count)

    def get_all_signatures(self):
        return set(chain.from_iterable(w.get_all_signatures()
                                       for w in self.id_worldBlocks.values()))

    def _sort_group(self):
        """Sorts WRLD groups by the FormID of the WRLD record."""
        self.id_worldBlocks = dict(dict_sort(self.id_worldBlocks))

    def updateMasters(self, masterset_add):
        """Updates set of master names according to masters actually used."""
        for worldBlock in self.id_worldBlocks.values():
            worldBlock.updateMasters(masterset_add)

    def updateRecords(self, srcBlock, mergeIds):
        """Updates any records in 'self' that exist in 'srcBlock'."""
        idGet = self.id_worldBlocks.get
        for wfid, srcWorldBlock in srcBlock.id_worldBlocks.items():
            worldBlock = idGet(wfid)
            if worldBlock:
                worldBlock.updateRecords(srcWorldBlock, mergeIds)

    def setWorld(self, world):
        """Adds a copy of the specified WRLD to this WRLDs block."""
        world_copy = world.getTypeCopy()
        wfid = world_copy.fid
        if wfid in self.id_worldBlocks:
            self.id_worldBlocks[wfid].world = world_copy
        else:
            children_head = ChildrenGrupHeader(0, DUMMY_FID, 1, self.stamp)
            worldBlock = MobWorld(children_head, self.loadFactory, world_copy)
            worldBlock.setChanged()
            self.id_worldBlocks[wfid] = worldBlock

    def remove_world(self, world):
        """Removes the specified world from this block. The exact world object
        must be present, otherwise a ValueError is raised."""
        del self.id_worldBlocks[world.fid]

    def iter_records(self):
        return chain.from_iterable(w.iter_records() for w in self.id_worldBlocks.values())

    def keepRecords(self, p_keep_ids):
        """Keeps records with fid in set p_keep_ids. Discards the rest."""
        for worldBlock in self.id_worldBlocks.values():
            worldBlock.keepRecords(p_keep_ids)
        self.id_worldBlocks = {k: x for k, x in self.id_worldBlocks.items() if
                               k in p_keep_ids}
        self.setChanged()

    def merge_records(self, block, loadSet, mergeIds, iiSkipMerge, doFilter):
        from ..mod_files import MasterSet # YUCK
        lookup_world_block = self.id_worldBlocks.get
        filtered_world_blocks = {}
        loadSetIsSuperset = loadSet.issuperset
        for src_fid, src_world_block in block.id_worldBlocks.items():
            was_newly_added = False
            # Check if we already have a world with that FormID
            dest_world_block = lookup_world_block(src_fid)
            if not dest_world_block:
                # We do not, add it and then look up again
                self.setWorld(src_world_block.world)
                dest_world_block = lookup_world_block(src_fid)
                was_newly_added = True
            # Delegate merging to the (potentially newly added) child world
            dest_world_block.merge_records(src_world_block, loadSet,
                mergeIds, iiSkipMerge, doFilter)
            # In IIM, skip all merging - note that we need to remove the child
            # world again if it was newly added in IIM mode.
            if iiSkipMerge:
                if was_newly_added:
                    self.remove_world(dest_world_block.world)
                continue
            # If we're Filter-tagged, check if the child world got filtered out
            if doFilter:
                masterset = MasterSet()
                src_world_block.updateMasters(masterset.add)
                if not loadSetIsSuperset(masterset):
                    # The child world got filtered out. If it was newly added,
                    # we need to remove it from this block again. Otherwise, we
                    # can just skip forward to the next child world.
                    if was_newly_added:
                        self.remove_world(dest_world_block.world)
                    continue
            # We're either not Filter-tagged or we want to keep this world
            filtered_world_blocks[src_fid] = src_world_block
        # Apply any merge filtering we've done above to the record block
        block.id_worldBlocks = filtered_world_blocks

    def __repr__(self):
        return u'<WRLD GRUP: %u record(s)>' % len(self.id_worldBlocks)
