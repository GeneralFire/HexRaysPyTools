import bisect
import idc
import idaapi
import re
import PySide.QtCore as QtCore
import PySide.QtGui as QtGui
from HexRaysPyTools.Forms import MyChoose

EA64 = idc.__EA64__
EA_SIZE = 8 if EA64 else 4
LEGAL_TYPES = ("_DWORD *", "int", "__int64", "signed __int64", "void *")


def parse_vtable_name(name):
    if name[0:3] == 'off':
        # off_XXXXXXXX case
        return "Vtable" + name[3:], False
    m = re.search(' (\w+)::', name)
    if m:
        # const class_name:`vftable' case
        return "Vtable_" + m.group(1), True
    return name, True


class AbstractField:
    def __init__(self, offset, scanned_variable, origin):
        """
        Offset is the very very base of the structure
        Origin is from which offset of the base structure the variable have been scanned
        scanned_variable - information about context in which this variable was scanned. This is necessary for final
        applying type after packing or finalizing structure.

        :param offset: int
        :param scanned_variable: ScannedVariable
        :param origin: int
        """
        self.offset = offset
        self.origin = origin
        self.enabled = True
        self.is_array = False
        self.scanned_variable = scanned_variable

    @property
    def type_name(self):
        pass

    __eq__ = lambda self, other: self.offset == other.offset and self.type_name == other.type_name
    __ne__ = lambda self, other: self.offset != other.offset or self.type_name != other.type_name
    __lt__ = lambda self, other: self.offset < other.offset or (self.offset == other.offset and self.type_name < other.type_name)
    __le__ = lambda self, other: self.offset <= other.offset
    __gt__ = lambda self, other: self.offset > other.offset or (self.offset == other.offset and self.type_name > other.type_name)
    __ge__ = lambda self, other: self.offset >= other.offset


class VirtualFunction:
    def __init__(self, address, offset):
        self.address = address
        self.offset = offset
        self.visited = False

    def __int__(self):
        return self.address

    def get_tinfo(self):
        decompiled_function = idaapi.decompile(self.address)
        if decompiled_function:
            tinfo = idaapi.tinfo_t(decompiled_function.type)
            tinfo.create_ptr(tinfo)
            return tinfo
        return None

    def get_udt_member(self):
        udt_member = idaapi.udt_member_t()
        udt_member.type = self.get_tinfo()
        udt_member.offset = self.offset
        udt_member.name = self.name
        udt_member.size = EA_SIZE
        return udt_member

    def get_information(self):
        return ["0x{0:08X}".format(self.address), self.name, self.get_tinfo().dstr()]

    @property
    def name(self):
        name = idaapi.get_short_name(self.address)
        name = name.split('(')[0]
        name = name.replace("`", '').replace(" ", '_').replace("'", '')
        return name


class VirtualTable(AbstractField):
    def __init__(self, offset, address, scanned_variable=None, origin=0):
        AbstractField.__init__(self, offset + origin, scanned_variable, origin)
        self.address = address
        self.virtual_functions = []
        self.name = "vtable"
        self.vtable_name, self.have_nice_name = parse_vtable_name(idaapi.get_short_name(address))
        self.populate()
        self.tinfo = self.create_tinfo()

    def populate(self):
        # TODO: Check if address of virtual function is in code section and then try to make function
        address = self.address
        while True:
            if EA64:
                func_address = idaapi.get_64bit(address)
            else:
                func_address = idaapi.get_32bit(address)
            flags = idaapi.getFlags(func_address)  # flags_t
            if idaapi.isCode(flags):
                self.virtual_functions.append(VirtualFunction(func_address, address - self.address))
                address += EA_SIZE
            else:
                break

    def create_tinfo(self):
        print "(Virtual table) at address: 0x{0:08X} name: {1}".format(self.address, self.name)
        udt_data = idaapi.udt_type_data_t()
        for function in self.virtual_functions:
            udt_data.push_back(function.get_udt_member())

        final_tinfo = idaapi.tinfo_t()
        if final_tinfo.create_udt(udt_data, idaapi.BTF_STRUCT):
            print "\n\t(Final structure)\n" + idaapi.print_tinfo('\t', 4, 5, idaapi.PRTYPE_MULTI | idaapi.PRTYPE_TYPE |
                                                                 idaapi.PRTYPE_SEMI, final_tinfo, self.name, None)
            return final_tinfo
        else:
            print "[ERROR] Virtual table creation failed"

    def import_to_structures(self, ask=False):
        """
        Imports virtual tables and returns tid_t of new structure

        :return: idaapi.tid_t
        """
        cdecl_typedef = idaapi.print_tinfo(None, 4, 5, idaapi.PRTYPE_MULTI | idaapi.PRTYPE_TYPE | idaapi.PRTYPE_SEMI,
                                           self.tinfo, self.vtable_name, None)
        if ask:
            cdecl_typedef = idaapi.asktext(0x10000, cdecl_typedef, "The following new type will be created")
            if not cdecl_typedef:
                return
        previous_ordinal = idaapi.get_type_ordinal(idaapi.cvar.idati, self.vtable_name)
        if previous_ordinal:
            idaapi.del_numbered_type(idaapi.cvar.idati, previous_ordinal)
            ordinal = idaapi.idc_set_local_type(previous_ordinal, cdecl_typedef, idaapi.PT_TYP)
        else:
            ordinal = idaapi.idc_set_local_type(-1, cdecl_typedef, idaapi.PT_TYP)

        if ordinal:
            print "[Info] Virtual table " + self.vtable_name + " added to Local Types"
            return idaapi.import_type(idaapi.cvar.idati, -1, self.vtable_name)
        else:
            print "[Warning] Virtual table " + self.vtable_name + " probably already exist"

    def get_udt_member(self, offset=0):
        udt_member = idaapi.udt_member_t()
        tid = self.import_to_structures()
        if tid != idaapi.BADADDR:
            udt_member.name = self.name
            tmp_tinfo = idaapi.create_typedef(self.vtable_name)
            tmp_tinfo.create_ptr(tmp_tinfo)
            udt_member.type = tmp_tinfo
            udt_member.offset = self.offset - offset
            udt_member.size = EA_SIZE
        return udt_member

    @staticmethod
    def check_address(address):
        # Checks if given address contains virtual table. Returns True if more than 2 function pointers found
        # Also if table's addresses point to code in executable section, than tries to make functions at that addresses
        functions_count = 0
        while True:
            func_address = idaapi.get_64bit(address) if EA64 else idaapi.get_32bit(address)
            flags = idaapi.getFlags(func_address)  # flags_t
            if idaapi.isCode(flags):
                functions_count += 1
                address += EA_SIZE
            else:
                segment = idaapi.getseg(func_address)
                if segment and segment.perm & idaapi.SEGPERM_EXEC:
                    if idc.MakeFunction(func_address):
                        functions_count += 1
                        address += EA_SIZE
                        continue
                break
            idaapi.autoWait()
        return functions_count >= 2

    @property
    def is_vtable(self): return True

    @property
    def type_name(self):
        return self.vtable_name + " *"

    @property
    def size(self):
        return EA_SIZE


class Field(AbstractField):
    def __init__(self, offset, tinfo, scanned_variable, origin=0):
        AbstractField.__init__(self, offset + origin, scanned_variable, origin)
        self.tinfo = tinfo
        self.name = "field_{0:X}".format(self.offset)

    def get_udt_member(self, array_size=0, offset=0):
        udt_member = idaapi.udt_member_t()
        udt_member.name = "field_{0:X}".format(self.offset - offset)
        udt_member.type = self.tinfo
        if array_size:
            tmp = idaapi.tinfo_t(self.tinfo)
            tmp.create_array(self.tinfo, array_size)
            udt_member.type = tmp
        udt_member.offset = self.offset - offset
        udt_member.size = self.size
        return udt_member

    @property
    def is_vtable(self): return False

    @property
    def type_name(self):
        return self.tinfo.dstr()

    @property
    def size(self):
        return self.tinfo.get_size()


class ScannedVariable:
    def __init__(self, function, variable):
        """
        Class for storing variable and it's function that have been scanned previously.
        Need to think whether it's better to store address and index, or cfunc_t and lvar_t

        :param function: idaapi.cfunc_t
        :param variable: idaapi.vdui_t
        """
        self.function = function
        self.lvar = variable

    def apply_type(self, tinfo):
        """
        Finally apply Class'es tinfo to this variable

        :param tinfo: idaapi.tinfo_t
        """
        hx_view = idaapi.open_pseudocode(self.function.entry_ea, -1)
        if hx_view:
            print "[Info] Applying tinfo to variable {0} in function {1}".format(
                self.lvar.name,
                idaapi.get_short_name(self.function.entry_ea)
            )
            # Finding lvar of new window that have the same name that saved one and applying tinfo_t
            lvar = filter(lambda x: x.name == self.lvar.name, hx_view.cfunc.get_lvars())[0]
            hx_view.set_lvar_type(lvar, tinfo)
            # idaapi.close_pseudocode(hx_view.form)
        else:
            print "[Warning] Failed to apply type"

    def __eq__(self, other):
        return self.function.entry_ea == other.function.entry_ea and self.lvar.name == other.lvar.name

    def __hash__(self):
        return hash((self.function.entry_ea, self.lvar.name))


class TemporaryStructureModel(QtCore.QAbstractTableModel):
    BYTE_TINFO = None

    def __init__(self, *args):
        """
        Keeps information about currently found fields in possible structure
        main_offset - is the base from where variables scanned. Can be set to different value if some field is passed by
                      reverence
        items - array of candidates to fields
        """
        super(TemporaryStructureModel, self).__init__(*args)
        self.main_offset = 0
        self.headers = ["Offset", "Type", "Name"]
        self.items = []
        self.collisions = []
        self.structure_name = "CHANGE_MY_NAME"
        TemporaryStructureModel.BYTE_TINFO = idaapi.tinfo_t(idaapi.BTF_BYTE)

    # OVERLOADED METHODS #

    def rowCount(self, *args):
        return len(self.items)

    def columnCount(self, *args):
        return len(self.headers)

    def data(self, index, role):
        row, col = index.row(), index.column()
        item = self.items[row]
        if role == QtCore.Qt.DisplayRole:
            if col == 0:
                return "0x{0:08X}".format(item.offset)
            elif col == 1:
                if not item.is_vtable and item.is_array and item.size > 0:
                    array_size = self.calculate_array_size(row)
                    if array_size:
                        return item.type_name + "[{}]".format(array_size)
                return item.type_name
            elif col == 2:
                return item.name
        elif role == QtCore.Qt.FontRole:
            if col == 1 and item.is_vtable:
                return QtGui.QFont("Consolas", 10, QtGui.QFont.Bold)
        elif role == QtCore.Qt.BackgroundColorRole:
            if not item.enabled:
                return QtGui.QColor(QtCore.Qt.gray)
            if item.offset == self.main_offset:
                if col == 0:
                    return QtGui.QBrush(QtGui.QColor("#ff8080"))
            if self.have_collision(row):
                return QtGui.QBrush(QtGui.QColor("#ffff99"))

    def headerData(self, section, orientation, role):
        if role == QtCore.Qt.DisplayRole and orientation == QtCore.Qt.Horizontal:
            return self.headers[section]

    # HELPER METHODS #

    def pack(self, start=0, stop=None):
        if self.collisions[start:stop].count(True):
            print "[Warning] Collisions detected"
            return

        final_tinfo = idaapi.tinfo_t()
        udt_data = idaapi.udt_type_data_t()
        origin = self.items[start].offset
        offset = origin

        for item in filter(lambda x: x.enabled, self.items[start:stop]):    # Filter disabled members
            gap_size = item.offset - offset
            if gap_size:
                udt_data.push_back(TemporaryStructureModel.get_padding_member(offset - origin, gap_size))
            if item.is_array:
                array_size = self.calculate_array_size(bisect.bisect_left(self.items, item))
                if array_size:
                    udt_data.push_back(item.get_udt_member(array_size, offset=origin))
                    offset = item.offset + item.size * array_size
                    continue
            udt_data.push_back(item.get_udt_member(offset=origin))
            offset = item.offset + item.size

        final_tinfo.create_udt(udt_data, idaapi.BTF_STRUCT)
        cdecl = idaapi.print_tinfo(None, 4, 5, idaapi.PRTYPE_MULTI | idaapi.PRTYPE_TYPE | idaapi.PRTYPE_SEMI,
                                   final_tinfo, self.structure_name, None)
        cdecl = idaapi.asktext(0x10000, cdecl, "The following new type will be created")

        if cdecl:
            structure_name = idaapi.idc_parse_decl(idaapi.cvar.idati, cdecl, idaapi.PT_TYP)[0]
            ordinal = idaapi.idc_set_local_type(-1, cdecl, idaapi.PT_TYP)
            if ordinal:
                print "[Info] New type {0} was added to Local Types".format(structure_name)
                tid = idaapi.import_type(idaapi.cvar.idati, -1, structure_name)
                if tid:
                    tinfo = idaapi.create_typedef(structure_name)
                    ptr_tinfo = idaapi.tinfo_t()
                    ptr_tinfo.create_ptr(tinfo)
                    for scanned_var in self.get_scanned_variables(origin):
                        scanned_var.apply_type(ptr_tinfo)
                    return tinfo
            else:
                print "[ERROR] Structure {0} probably already exist".format(structure_name)
        return None

    def have_member(self, member):
        if self.items:
            idx = bisect.bisect_left(self.items, member)
            if idx < self.rowCount():
                return self.items[bisect.bisect_left(self.items, member)] == member
        return False

    def have_collision(self, row):
        return self.collisions[row]

    def refresh_collisions(self):
        self.collisions = [False for _ in xrange(len(self.items))]
        if (len(self.items)) > 1:
            curr = 0
            while curr < len(self.items):
                if self.items[curr].enabled:
                    break
                curr += 1
            next = curr + 1
            while next < len(self.items):
                if self.items[next].enabled:
                    if self.items[curr].offset + self.items[curr].size > self.items[next].offset:
                        self.collisions[curr] = True
                        self.collisions[next] = True
                        if self.items[curr].offset + self.items[curr].size < self.items[next].offset + self.items[next].size:
                            curr = next
                    else:
                        curr = next
                next += 1

    def add_row(self, member):
        if not self.have_member(member):
            bisect.insort(self.items, member)
            self.refresh_collisions()
            self.modelReset.emit()

    def get_scanned_variables(self, origin=0):
        return set(
            map(lambda x: x.scanned_variable, filter(lambda x: x.scanned_variable and x.origin == origin, self.items))
        )

    def get_next_enabled(self, row):
        row += 1
        while row < self.rowCount():
            if self.items[row].enabled:
                return row
            row += 1
        return None

    def calculate_array_size(self, row):
        next_row = self.get_next_enabled(row)
        if next_row:
            return (self.items[next_row].offset - self.items[row].offset) / self.items[row].size
        return 0

    @staticmethod
    def get_padding_member(offset, size):
        udt_member = idaapi.udt_member_t()
        if size == 1:
            udt_member.name = "gap_{0:X}".format(offset)
            udt_member.type = TemporaryStructureModel.BYTE_TINFO
            udt_member.size = TemporaryStructureModel.BYTE_TINFO.get_size()
            udt_member.offset = offset
            return udt_member

        array_data = idaapi.array_type_data_t()
        array_data.base = 0
        array_data.elem_type = TemporaryStructureModel.BYTE_TINFO
        array_data.nelems = size
        tmp_tinfo = idaapi.tinfo_t()
        tmp_tinfo.create_array(array_data)

        udt_member.name = "gap_{0:X}".format(offset)
        udt_member.type = tmp_tinfo
        udt_member.size = size
        udt_member.offset = offset
        return udt_member

    # SLOTS #

    def finalize(self):
        if self.pack():
            self.clear()

    def disable_rows(self, indices):
        for idx in indices:
            if self.items[idx.row()].enabled:
                self.items[idx.row()].enabled = False
                self.items[idx.row()].is_array = False
        self.refresh_collisions()
        self.modelReset.emit()

    def enable_rows(self, indices):
        for idx in indices:
            if not self.items[idx.row()].enabled:
                self.items[idx.row()].enabled = True
        self.refresh_collisions()
        self.modelReset.emit()

    def set_origin(self, indices):
        if indices:
            self.main_offset = self.items[indices[0].row()].offset
            self.modelReset.emit()

    def make_array(self, indices):
        if indices:
            item = self.items[indices[0].row()]
            if not item.is_vtable:
                item.is_array ^= True
                self.modelReset.emit()

    def pack_substructure(self, indices):
        if indices:
            indices = list(map(lambda x: x.row(), indices))
            indices.sort()
            start, stop = indices[0], indices[-1] + 1
            tinfo = self.pack(start, stop)
            if tinfo:
                offset = self.items[start].offset
                self.items = self.items[0:start] + self.items[stop:]
                self.add_row(Field(offset, tinfo, None))

    def remove_item(self, indices):
        rows = map(lambda x: x.row(), indices)
        if rows:
            self.items = [item for item in self.items if self.items.index(item) not in rows]
            self.modelReset.emit()

    def clear(self):
        self.items = []
        self.main_offset = 0
        self.modelReset.emit()

    def show_virtual_methods(self, index):
        self.dataChanged.emit(index, index)

        if index.column() == 1:
            item = self.items[index.row()]
            if item.is_vtable:
                function_chooser = MyChoose(
                    [function.get_information() for function in item.virtual_functions],
                    "Select Virtual Function",
                    [["Address", 5], ["Name", 15], ["Declaration", 30]],
                    13
                )
                function_chooser.OnGetIcon = lambda n: 32 if item.virtual_functions[n].visited else 160
                function_chooser.OnGetLineAttr = \
                    lambda n: [0xd9d9d9, 0x0] if item.virtual_functions[n].visited else [0xffffff, 0x0]

                idx = function_chooser.Show(True)
                if idx != -1:
                    item.virtual_functions[idx].visited = True
                    idaapi.open_pseudocode(int(item.virtual_functions[idx]), 1)