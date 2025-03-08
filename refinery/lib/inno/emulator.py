#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
An emulator for Inno Setup executables. The implementation is unlikely to be 100% correct as it
was engineered by making various malicious scripts execute reasonably well, not by implementing
an exact copy of [the (only) reference implementation][PS]. This is grew and grew as I wrote it
and seems mildly insane in hindsight.

[PS]: https://github.com/remobjects/pascalscript
"""
from __future__ import annotations

from typing import (
    get_origin,
    Any,
    Callable,
    ClassVar,
    Dict,
    Generic,
    List,
    NamedTuple,
    Optional,
    Sequence,
    TypeVar,
    Union,
)

from dataclasses import dataclass, field
from enum import auto, Enum, IntFlag
from functools import partial
from pathlib import Path
from string import Formatter
from time import process_time
from urllib.parse import unquote

from refinery.lib.tools import cached_property
from refinery.lib.types import CaseInsensitiveDict
from refinery.lib.inno.archive import InnoArchive, Flags
from refinery.lib.types import AST, INF, NoMask
from refinery.lib.patterns import formats

from refinery.lib.inno.ifps import (
    AOp,
    COp,
    EHType,
    Function,
    IFPSFile,
    IFPSType,
    Op,
    Operand,
    OperandType,
    TArray,
    TC,
    TRecord,
    TStaticArray,
    Value,
    VariableBase,
    Variant,
    VariantType,
)

import fnmatch
import hashlib
import inspect
import io
import math
import operator
import random
import re
import struct
import time


_T = TypeVar('_T')


class InvalidIndex(TypeError):
    def __init__(self, v: Variable, key):
        super().__init__(F'Assigning to {v.spec}[{key!r}]; type {v.type} does not support indexing.')


class NullPointer(RuntimeError):
    def __init__(self, v: Variable):
        super().__init__(F'Trying to access uninitialized pointer value {v.spec}.')


class OleObject:
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return F'OleObject({self.name!r})'

    def __str__(self):
        return self.name


class Variable(VariableBase, Generic[_T]):
    type: IFPSType
    spec: Optional[Variant]
    data: Optional[Union[List[Variable], _T]]
    path: tuple[int]

    @property
    def container(self):
        return self.type.container

    @property
    def pointer(self):
        return self.type.code == TC.Pointer

    def __len__(self):
        return len(self.data)

    def __bool__(self):
        return True

    def __getitem__(self, key: int):
        var = self.deref()
        if var.container:
            return var.at(key).get()
        else:
            return var.data[key]

    def __setitem__(self, key: int, v: _T):
        var = self.deref()
        if var.container:
            var.at(key).set(v)
        else:
            var.data[key] = var._wrap(v)

    def __index__(self):
        data = self.data
        if isinstance(data, str) and len(data) == 1:
            data = ord(data)
        return data

    def at(self, k: int):
        return self.deref().data[k]

    def deref(var):
        while True:
            val = var.data
            if not isinstance(val, Variable):
                return var
            var = val

    def __init__(
        self,
        type: IFPSType,
        spec: Optional[Variant] = None,
        path: tuple[int] = (),
        data: Optional[Union[_T, List]] = None
    ):
        super().__init__(type, spec)
        self.path = path

        self._int_size = _size = {
            TC.U08: +1,
            TC.U16: +1,
            TC.U32: +1,
            TC.S08: -1,
            TC.S16: -1,
            TC.S32: -1,
            TC.S64: -1,
        }.get((code := type.code), 0) * code.width
        if _size:
            bits = abs(_size) * 8
            umax = (1 << bits)
            self._int_bits = bits
            self._int_mask = umax - 1
            if _size < 0:
                self._int_good = range(-(umax >> 1), (umax >> 1))
            else:
                self._int_good = range(umax)
        else:
            self._int_mask = NoMask
            self._int_bits = INF
            self._int_good = AST

        if data is None:
            def default(type: IFPSType, *sub_path):
                if isinstance(type, TRecord):
                    return [Variable(t, spec, (*path, *sub_path, k)) for k, t in enumerate(type.members)]
                if isinstance(type, TStaticArray):
                    t = type.type
                    return [Variable(t, spec, (*path, *sub_path, k)) for k in range(type.size)]
                if isinstance(type, TArray):
                    return []
                if sub_path:
                    return Variable(type, spec, (*path, *sub_path))
                else:
                    return type.default()
            self.data = default(type)
        else:
            self.set(data)

    def _wrap(self, value: Union[Value, _T], key: Optional[int] = None) -> _T:
        if (t := self.type.py_type(key)) and not isinstance(value, t):
            if issubclass(t, int):
                if isinstance(value, str) and len(value) == 1:
                    return ord(value[0])
                if isinstance(value, float):
                    return int(value)
            elif isinstance(value, int):
                if issubclass(t, str):
                    return chr(value)
                if issubclass(t, float):
                    return float(value)
            raise TypeError(F'Assigning value {value!r} to variable of type {self.type}.')
        if s := self._int_size and value not in self._int_good:
            mask = self._int_mask
            value &= mask
            if s < 0 and (value >> (self._int_bits - 1)):
                value = -(-value & mask)
        return value

    def resize(self, n: int):
        t = self.type
        m = n - len(self.data)
        if t.code != TC.Array:
            if t.code not in (TC.StaticArray, TC.Record):
                raise TypeError
            if n == t.size:
                return
            raise ValueError(F'Attempt to resize {t} of size {t.size} to {n}.')
        if m <= 0:
            del self.data[n:]
            return
        for k in range(m):
            self.data.append(Variable(t.type, self.spec, (*self.path, k)))

    def setptr(self, var: Variable, copy: bool = False):
        if not self.pointer:
            raise TypeError
        if not isinstance(var, Variable):
            raise TypeError
        if copy:
            var = Variable(var.type, data=var.get())
        self.data = var

    def set(
        self,
        value: Union[_T, Sequence, Variable],
    ):
        if isinstance(value, Variable):
            if value.container:
                dst = self.deref()
                if not dst.container:
                    raise TypeError(F'Attempting to assign container type {value.type} to non-container {self.type}.')
                dst.resize(len(value))
                for k, v in enumerate(value.data):
                    dst.data[k].set(v)
                return
            if value.pointer:
                if self.pointer:
                    self.data = value.data
                else:
                    self.set(value.deref())
                return
            value = value.get()
        if isinstance(value, (Enum, Value)):
            value = value.value
        if self.pointer:
            return self.deref().set(value)
        elif self.container:
            if not isinstance(value, (list, tuple)):
                raise TypeError
            self.resize(len(value))
            for k, v in enumerate(value):
                self.data[k].set(v)
        else:
            self.data = self._wrap(value)

    def get(self) -> _T:
        if self.pointer:
            return self.deref().get()
        if self.container:
            data: List[Variable] = self.data
            return [v.get() for v in data]
        return self.data

    @property
    def name(self):
        if self.spec is None:
            return 'Unbound'
        name = F'{self.spec!s}'
        for k in self.path:
            name = F'{name}[{k}]'
        return name

    def __repr__(self):
        rep = self.name
        if (val := self.data) is None:
            return rep
        if self.type.code is TC.Set:
            val = F'{val:b}'
        elif self.pointer:
            val: Variable
            return F'{rep} -> {val.name}'
        elif isinstance(val, (str, int, float, list)):
            val = repr(self.get())
        else:
            return rep
        return F'{rep} = {val}'


class NeedSymbol(NotImplementedError):
    pass


class OpCodeNotImplemented(NotImplementedError):
    pass


class EmulatorException(RuntimeError):
    pass


class AbortEmulation(Exception):
    pass


class IFPSException(RuntimeError):
    def __init__(self, msg: str, parent: Optional[BaseException] = None):
        super().__init__(msg)
        self.parent = parent


class EmulatorTimeout(TimeoutError):
    pass


class EmulatorExecutionLimit(TimeoutError):
    pass


class EmulatorMaxStack(MemoryError):
    pass


class EmulatorMaxCalls(MemoryError):
    pass


class IFPS_NotAnArray(RuntimeError):
    def __init__(self, v: Variable):
        super().__init__(F'Attempting an array operation on non-array variable {v}.')


@dataclass
class ExceptionHandler:
    finally_one: Optional[int]
    catch_error: Optional[int]
    finally_two: Optional[int]
    handler_end: int
    current: EHType = EHType.Try


class IFPSEmulatedFunction(NamedTuple):
    call: Callable
    spec: List[bool]
    static: bool
    void: bool = False

    @property
    def argc(self):
        return len(self.spec)


@dataclass
class IFPSEmulatorConfig:
    x64: bool = True
    admin: bool = True
    windows_os_version: tuple[int, int, int] = (10, 0, 10240)
    windows_sp_version: tuple[int, int] = (2, 0)
    throw_abort: bool = False
    trace_calls: bool = False
    log_passwords: bool = True
    wizard_silent: bool = True
    max_opcodes: int = 0
    max_seconds: int = 60
    sleep_scale: float = 0.0
    max_data_stack: int = 1_000_000
    max_call_stack: int = 4096
    environment: dict[str, str] = field(default_factory=dict)
    user_name: str = 'Frank'
    host_name: str = 'Frank-PC'
    inno_name: str = 'ThisInstall'
    language: str = 'en'
    executable: str = 'C:\\Install.exe'
    install_to: str = 'I:\\'
    lcid: int = 0x0409

    @property
    def cwd(self):
        return Path(self.executable).parent


class TSetupStep(int, Enum):
    ssPreInstall = 0
    ssInstall = auto()
    ssPostInstall = auto()
    ssDone = auto()


class TSplitType(int, Enum):
    stAll = 0
    stExcludeEmpty = auto()
    stExcludeLastEmpty = auto()


class TUninstallStep(int, Enum):
    usAppMutexCheck = 0
    usUninstall = auto()
    usPostUninstall = auto()
    usDone = auto()


class TSetupProcessorArchitecture(int, Enum):
    paUnknown = 0
    paX86 = auto()
    paX64 = auto()
    paArm32 = auto()
    paArm64 = auto()


class PageID(int, Enum):
    wpWelcome = 1
    wpLicense = auto()
    wpPassword = auto()
    wpInfoBefore = auto()
    wpUserInfo = auto()
    wpSelectDir = auto()
    wpSelectComponents = auto()
    wpSelectProgramGroup = auto()
    wpSelectTasks = auto()
    wpReady = auto()
    wpPreparing = auto()
    wpInstalling = auto()
    wpInfoAfter = auto()
    wpFinished = auto()


class IFPSCall(NamedTuple):
    name: str
    args: tuple


class FPUControl(IntFlag):
    InvalidOperation    = 0b0_00_0_00_00_00_000001 # noqa
    DenormalizedOperand = 0b0_00_0_00_00_00_000010 # noqa
    ZeroDivide          = 0b0_00_0_00_00_00_000100 # noqa
    Overflow            = 0b0_00_0_00_00_00_001000 # noqa
    Underflow           = 0b0_00_0_00_00_00_010000 # noqa
    PrecisionError      = 0b0_00_0_00_00_00_100000 # noqa
    Reserved1           = 0b0_00_0_00_00_01_000000 # noqa
    Reserved2           = 0b0_00_0_00_00_10_000000 # noqa
    ExtendPrecision     = 0b0_00_0_00_01_00_000000 # noqa
    DoublePrecision     = 0b0_00_0_00_10_00_000000 # noqa
    MaxPrecision        = 0b0_00_0_00_11_00_000000 # noqa
    RoundDown           = 0b0_00_0_01_00_00_000000 # noqa
    RoundUp             = 0b0_00_0_10_00_00_000000 # noqa
    RoundTowardZero     = 0b0_00_0_11_00_00_000000 # noqa
    AffineInfinity      = 0b0_00_1_00_00_00_000000 # noqa
    Reserved3           = 0b0_01_0_00_00_00_000000 # noqa
    Reserved4           = 0b0_10_0_00_00_00_000000 # noqa
    ReservedBits        = 0b0_11_0_00_00_11_000000 # noqa


class IFPSEmulator:

    def __init__(
        self,
        archive: Union[InnoArchive, IFPSFile],
        options: Optional[IFPSEmulatorConfig] = None,
        **more
    ):
        if isinstance(archive, InnoArchive):
            self.inno = archive
            self.ifps = ifps = archive.ifps
        else:
            self.inno = None
            self.ifps = archive
        self.config = options or IFPSEmulatorConfig(**more)
        self.globals = [Variable(v.type, v.spec) for v in ifps.globals]
        self.stack: List[Variable] = []
        self.trace: List[IFPSCall] = []
        self.passwords: set[str] = set()
        self.jumpflag = False
        self.fpucw = FPUControl(0)
        self.mutexes: set[str] = set()
        self.symbols: dict[str, Function] = CaseInsensitiveDict()
        for pfn in ifps.functions:
            self.symbols[pfn.name] = pfn

    def __repr__(self):
        return self.__class__.__name__

    def unimplemented(self, function: Function):
        raise NeedSymbol(function.name)

    def emulate_function(self, function: Function, *args):
        self.stack.clear()
        decl = function.decl
        if decl is None:
            raise NotImplementedError(F'Do not know how to call {function!s}.')
        if (n := len(decl.parameters)) != (m := len(args)):
            raise ValueError(
                F'Function {function!s} expects {n} arguments, only {m} were given.')
        for index, (argument, parameter) in enumerate(zip(args, decl.parameters), 1):
            variable = Variable(parameter.type, Variant(index, VariantType.Local))
            variable.set(argument)
            self.stack.append(variable)
        self.stack.reverse()
        if not decl.void:
            result = Variable(decl.return_type, Variant(0, VariantType.Argument))
            self.stack.append(result)
        self.call(function)
        self.stack.clear()
        if not decl.void:
            return result.get()

    def call(self, function: Function):
        def operator_div(a, b):
            return a // b if isinstance(a, int) and isinstance(b, int) else a / b

        def operator_in(a, b):
            return a in b

        def getvar(op: Union[Variant, Operand]) -> Variable:
            if not isinstance(op, Operand):
                v = op
                k = None
            elif op.type is OperandType.Value:
                raise TypeError('Attempting to retrieve variable for an immediate operand.')
            else:
                v = op.variant
                k = op.index
                if op.type is OperandType.IndexedByVar:
                    k = getvar(k).get()
            t, i = v.type, v.index
            if t is VariantType.Argument:
                if function.decl.void:
                    i -= 1
                var = self.stack[sp - i]
            elif t is VariantType.Global:
                var = self.globals[i]
            elif t is VariantType.Local:
                var = self.stack[sp + i]
            else:
                raise TypeError
            if k is not None:
                var = var.at(k)
            return var

        def getval(op: Operand):
            if op.immediate:
                return op.value.value
            return getvar(op).get()

        def setval(op: Operand, new):
            if op.immediate:
                raise RuntimeError('attempt to assign to an immediate')
            getvar(op).set(new)

        class CallState(NamedTuple):
            fn: Function
            ip: int
            sp: int
            eh: List[ExceptionHandler]

        callstack: List[CallState] = []

        cycle = 0
        exec_start = process_time()

        ip: int = 0
        sp: int = len(self.stack) - 1
        pending_exception = None
        exceptions = []

        while True:
            if 0 < self.config.max_data_stack < len(callstack):
                raise EmulatorMaxCalls

            if function.body is None:
                decl = function.decl
                name = function.name
                tcls = decl and (decl.classname or decl.module)
                tcls = tcls or ''
                registry: dict[str, IFPSEmulatedFunction] = self.external_symbols.get(tcls, {})
                handler = registry.get(name)

                if handler:
                    void = handler.void
                    argc = handler.argc
                elif decl:
                    void = decl.void
                    argc = decl.argc
                else:
                    void = True
                    argc = 0

                try:
                    rpos = 0 if void else 1
                    args = [self.stack[~k] for k in range(rpos, argc + rpos)]
                except IndexError:
                    raise EmulatorException(
                        F'Cannot call {function!s}; {argc} arguments + {rpos} return values expected,'
                        F' but stack size is only {len(self.stack)}.')

                if self.config.trace_calls:
                    self.trace.append(IFPSCall(str(function), tuple(a.get() for a in args)))

                if handler is None:
                    self.unimplemented(function)
                else:
                    if decl and (decl.void != handler.void or decl.argc != handler.argc):
                        raise RuntimeError(F'Handler for {function!s} does not match the declaration.')
                    for k, (var, byref) in enumerate(zip(args, handler.spec)):
                        if not byref:
                            args[k] = var.get()
                    if not handler.static:
                        args.insert(0, self)
                    try:
                        return_value = handler.call(*args)
                    except BaseException as b:
                        pending_exception = IFPSException(F'Error calling {function.name}: {b!s}', b)
                    else:
                        if not handler.void:
                            self.stack[-1].set(return_value)
                if not callstack:
                    if pending_exception is None:
                        return
                    raise pending_exception
                function, ip, sp, exceptions = callstack.pop()
                continue

            while insn := function.code.get(ip, None):
                if 0 < self.config.max_seconds < process_time() - exec_start:
                    raise EmulatorTimeout
                if 0 < self.config.max_opcodes < cycle:
                    raise EmulatorExecutionLimit
                if 0 < self.config.max_data_stack < len(self.stack):
                    raise EmulatorMaxStack
                try:
                    if pe := pending_exception:
                        pending_exception = None
                        raise pe

                    opc = insn.opcode
                    ip += insn.size
                    cycle += 1

                    if opc == Op.Nop:
                        continue
                    elif opc == Op.Assign:
                        dst = getvar(insn.op(0))
                        src = insn.op(1)
                        if src.immediate:
                            dst.set(src.value)
                        else:
                            dst.set(getvar(src))
                    elif opc == Op.Calculate:
                        calculate = {
                            AOp.Add: operator.add,
                            AOp.Sub: operator.sub,
                            AOp.Mul: operator.mul,
                            AOp.Div: operator_div,
                            AOp.Mod: operator.mod,
                            AOp.Shl: operator.lshift,
                            AOp.Shr: operator.rshift,
                            AOp.And: operator.and_,
                            AOp.BOr: operator.or_,
                            AOp.Xor: operator.xor,
                        }[insn.operator]
                        src = insn.op(1)
                        dst = insn.op(0)
                        sv = getval(src)
                        dv = getval(dst)
                        fpu = isinstance(sv, float) or isinstance(dv, float)
                        try:
                            result = calculate(dv, sv)
                            if fpu and not isinstance(result, float):
                                raise FloatingPointError
                        except FloatingPointError as FPE:
                            if not self.fpucw & FPUControl.InvalidOperation:
                                result = float('nan')
                            else:
                                raise IFPSException('invalid operation', FPE) from FPE
                        except OverflowError as OFE:
                            if fpu and self.fpucw & FPUControl.Overflow:
                                result = float('nan')
                            else:
                                raise IFPSException('arithmetic overflow', OFE) from OFE
                        except ZeroDivisionError as ZDE:
                            if fpu and self.fpucw & FPUControl.ZeroDivide:
                                result = float('nan')
                            else:
                                raise IFPSException('division by zero', ZDE) from ZDE
                        setval(dst, result)
                    elif opc == Op.Push:
                        # TODO: I do not actually know how this works
                        self.stack.append(getval(insn.op(0)))
                    elif opc == Op.PushVar:
                        self.stack.append(getvar(insn.op(0)))
                    elif opc == Op.Pop:
                        self.temp = self.stack.pop()
                    elif opc == Op.Call:
                        callstack.append(CallState(function, ip, sp, exceptions))
                        function = insn.operands[0]
                        ip = 0
                        sp = len(self.stack) - 1
                        exceptions = []
                        break
                    elif opc == Op.Jump:
                        ip = insn.operands[0]
                    elif opc == Op.JumpTrue:
                        if getval(insn.op(1)):
                            ip = insn.operands[0]
                    elif opc == Op.JumpFalse:
                        if not getval(insn.op(1)):
                            ip = insn.operands[0]
                    elif opc == Op.Ret:
                        del self.stack[sp + 1:]
                        if not callstack:
                            return
                        function, ip, sp, exceptions = callstack.pop()
                        break
                    elif opc == Op.StackType:
                        raise OpCodeNotImplemented(str(opc))
                    elif opc == Op.PushType:
                        self.stack.append(Variable(
                            insn.operands[0],
                            Variant(len(self.stack) - sp, VariantType.Local)
                        ))
                    elif opc == Op.Compare:
                        compare = {
                            COp.GE: operator.ge,
                            COp.LE: operator.le,
                            COp.GT: operator.gt,
                            COp.LT: operator.lt,
                            COp.NE: operator.ne,
                            COp.EQ: operator.eq,
                            COp.IN: operator_in,
                            COp.IS: operator.is_,
                        }[insn.operator]
                        d = getvar(insn.op(0))
                        a = getval(insn.op(1))
                        b = getval(insn.op(2))
                        d.set(compare(a, b))
                    elif opc == Op.CallVar:
                        pfn = getval(insn.op(0))
                        if isinstance(pfn, int):
                            pfn = self.ifps.functions[pfn]
                        if isinstance(pfn, Function):
                            self.call(pfn)
                    elif opc in (Op.SetPtr, Op.SetPtrToCopy):
                        copy = False
                        if opc == Op.SetPtrToCopy:
                            copy = True
                        dst = getvar(insn.op(0))
                        src = getvar(insn.op(1))
                        dst.setptr(src, copy=copy)
                    elif opc == Op.BooleanNot:
                        setval(a := insn.op(0), not getval(a))
                    elif opc == Op.IntegerNot:
                        setval(a := insn.op(0), ~getval(a))
                    elif opc == Op.Neg:
                        setval(a := insn.op(0), -getval(a))
                    elif opc == Op.SetFlag:
                        condition, negated = insn.operands
                        self.jumpflag = getval(condition) ^ negated
                    elif opc == Op.JumpFlag:
                        if self.jumpflag:
                            ip = insn.operands[0]
                    elif opc == Op.PushEH:
                        exceptions.append(ExceptionHandler(*insn.operands))
                    elif opc == Op.PopEH:
                        tp = None
                        et = EHType(insn.operands[0])
                        eh = exceptions[-1]
                        if eh.current != et:
                            raise RuntimeError(F'Expected {eh.current} block to end, but {et} was ended instead.')
                        while tp is None:
                            if et is None:
                                raise RuntimeError
                            tp, et = {
                                EHType.Catch         : (eh.finally_one, EHType.Finally),
                                EHType.Try           : (eh.finally_one, EHType.Finally),
                                EHType.Finally       : (eh.finally_two, EHType.SecondFinally),
                                EHType.SecondFinally : (eh.handler_end, None),
                            }[et]
                        eh.current = et
                        ip = tp
                        if et is None:
                            exceptions.pop()
                    elif opc == Op.Inc:
                        setval(a := insn.op(0), getval(a) + 1)
                    elif opc == Op.Dec:
                        setval(a := insn.op(0), getval(a) - 1)
                    elif opc == Op.JumpPop1:
                        self.stack.pop()
                        ip = insn.operands[0]
                    elif opc == Op.JumpPop2:
                        self.stack.pop()
                        self.stack.pop()
                        ip = insn.operands[0]
                    else:
                        raise RuntimeError(F'Function contains invalid opcode at 0x{ip:X}.')
                except IFPSException as EE:
                    try:
                        eh = exceptions[-1]
                    except IndexError:
                        raise EE
                    et = EHType.Try
                    tp = None
                    while tp is None:
                        if et is None:
                            raise RuntimeError
                        tp, et = {
                            EHType.Try           : (eh.catch_error, EHType.Catch),
                            EHType.Catch         : (eh.finally_one, EHType.Finally),
                            EHType.Finally       : (eh.finally_two, EHType.SecondFinally),
                            EHType.SecondFinally : (eh.handler_end, None),
                        }[et]
                    if et is None:
                        raise EE
                    eh.current = et
                    ip = tp
                except AbortEmulation:
                    raise
                except EmulatorException:
                    raise
                # except Exception as RE:
                #     raise EmulatorException(
                #         F'In {function.symbol} at 0x{insn.offset:X} (cycle {cycle}), '
                #         F'emulation of {insn!r} failed: {RE!s}')
            if ip is None:
                raise RuntimeError(F'Instruction pointer moved out of bounds to 0x{ip:X}.')

    external_symbols: ClassVar[
        Dict[str,                        # class name for methods or empty string for functions
        Dict[str, IFPSEmulatedFunction]] # method or function name to emulation info
    ] = CaseInsensitiveDict()

    def external(*args, static=True, __reg: dict = external_symbols, **kwargs):
        def decorator(pfn):
            signature = inspect.signature(pfn)
            name: str = kwargs.get('name', pfn.__name__)
            csep: str = '.'
            if csep not in name:
                csep = '__'
            classname, _, name = name.rpartition(csep)
            if (registry := __reg.get(classname)) is None:
                registry = __reg[classname] = CaseInsensitiveDict()
            void = kwargs.get('void', signature.return_annotation == signature.empty)
            parameters: List[bool] = []
            specs = iter(signature.parameters.values())
            if not static:
                next(specs)
            for spec in specs:
                try:
                    hint = eval(spec.annotation)
                except Exception as E:
                    raise RuntimeError(F'Invalid signature: {signature}') from E
                if not isinstance(hint, type):
                    hint = get_origin(hint)
                parameters.append(issubclass(hint, Variable))
            registry[name] = e = IFPSEmulatedFunction(pfn, parameters, static, void)
            aliases = kwargs.get('alias', [])
            if isinstance(aliases, str):
                aliases = [aliases]
            for name in aliases:
                registry[name] = e
            if static:
                pfn = staticmethod(pfn)
            return pfn
        return decorator(args[0]) if args else decorator

    @external(static=False)
    def TPasswordEdit__Text(self, value: str) -> str:
        if value:
            self.passwords.add(value)
        return value

    @external
    def kernel32__GetTickCount() -> int:
        return time.monotonic_ns()

    @external
    def user32__GetSystemMetrics(index: int) -> int:
        if index == 80:
            return 1
        if index == 43:
            return 2
        return 0

    @external
    def IsX86Compatible() -> bool:
        return True

    @external(alias=[
        'sArm64',
        'IsArm32Compatible',
        'Debugging',
        'IsUninstaller',
    ])
    def Terminated() -> bool:
        return False

    @external(static=False)
    def IsAdmin(self) -> bool:
        return self.config.admin

    @external(static=False)
    def Sleep(self, ms: int):
        time.sleep(ms * self.config.sleep_scale / 1000.0)

    @external
    def Random(top: int) -> int:
        return random.randrange(0, top)

    @external(alias='StrGet')
    def WStrGet(string: Variable[str], index: int) -> str:
        if index <= 0:
            raise ValueError
        return string[index - 1:index]

    @external(alias='StrSet')
    def WStrSet(char: str, index: int, dst: Variable[str]):
        old = dst.get()
        index -= 1
        dst.set(old[:index] + char + old[index:])

    @external(static=False)
    def GetEnv(self, name: str) -> str:
        return self.config.environment.get(name, F'%{name}%')

    @external
    def Beep():
        pass

    @external(static=False)
    def Abort(self):
        if self.config.throw_abort:
            raise AbortEmulation

    @external
    def DirExists(path: str) -> bool:
        return True

    @external
    def ForceDirectories(path: str) -> bool:
        return True

    @external(alias='LoadStringFromLockedFile')
    def LoadStringFromFile(path: str, out: Variable[str]) -> bool:
        return True

    @external(alias='LoadStringsFromLockedFile')
    def LoadStringsFromFile(path: str, out: Variable[str]) -> bool:
        return True

    @cached_property
    def constant_map(self) -> dict[str, str]:
        tmp = random.choices('ABCDEFGHIJKLMNOPQRSTUVWXYZ', k=5)
        cfg = self.config
        map = {
            'app'               : cfg.install_to,
            'win'               : R'C:\Windows',
            'sys'               : R'C:\Windows\System',
            'sysnative'         : R'C:\Windows\System32',
            'src'               : str(Path(cfg.executable).parent),
            'sd'                : R'C:',
            'commonpf'          : R'C:\Program Files',
            'commoncf'          : R'C:\Program Files\Common Files',
            'tmp'               : RF'C:\Windows\Temp\IS-{tmp}',
            'commonfonts'       : R'C:\Windows\Fonts',
            'dao'               : R'C:\Program Files\Common Files\Microsoft Shared\DAO',
            'dotnet11'          : R'C:\Windows\Microsoft.NET\Framework\v1.1.4322',
            'dotnet20'          : R'C:\Windows\Microsoft.NET\Framework\v3.0',
            'dotnet2032'        : R'C:\Windows\Microsoft.NET\Framework\v3.0',
            'dotnet40'          : R'C:\Windows\Microsoft.NET\Framework\v4.0.30319',
            'dotnet4032'        : R'C:\Windows\Microsoft.NET\Framework\v4.0.30319',
            'group'             : RF'C:\Users\{cfg.user_name}\Start Menu\Programs\{cfg.inno_name}',
            'localappdata'      : RF'C:\Users\{cfg.user_name}\AppData\Local',
            'userappdata'       : RF'C:\Users\{cfg.user_name}\AppData\Roaming',
            'userdesktop'       : RF'C:\Users\{cfg.user_name}\Desktop',
            'userdocs'          : RF'C:\Users\{cfg.user_name}\Documents',
            'userfavourites'    : RF'C:\Users\{cfg.user_name}\Favourites',
            'usersavedgames'    : RF'C:\Users\{cfg.user_name}\Saved Games',
            'usersendto'        : RF'C:\Users\{cfg.user_name}\SendTo',
            'userstartmenu'     : RF'C:\Users\{cfg.user_name}\Start Menu',
            'userprograms'      : RF'C:\Users\{cfg.user_name}\Start Menu\Programs',
            'userstartup'       : RF'C:\Users\{cfg.user_name}\Start Menu\Programs\Startup',
            'usertemplates'     : RF'C:\Users\{cfg.user_name}\Templates',
            'commonappdata'     : R'C:\ProgramData',
            'commondesktop'     : R'C:\ProgramData\Microsoft\Windows\Desktop',
            'commondocs'        : R'C:\ProgramData\Microsoft\Windows\Documents',
            'commonstartmenu'   : R'C:\ProgramData\Microsoft\Windows\Start Menu',
            'commonprograms'    : R'C:\ProgramData\Microsoft\Windows\Start Menu\Programs',
            'commonstartup'     : R'C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Startup',
            'commontemplates'   : R'C:\ProgramData\Microsoft\Windows\Templates',
            'cmd'               : R'C:\Windows\System32\cmd.exe',
            'computername'      : cfg.host_name,
            'groupname'         : cfg.inno_name,
            'hwnd'              : '0',
            'wizardhwnd'        : '0',
            'language'          : cfg.language,
            'srcexe'            : cfg.executable,
            'sysuserinfoname'   : '{sysuserinfoname}',
            'sysuserinfoorg'    : '{sysuserinfoorg}',
            'userinfoname'      : '{userinfoname}',
            'userinfoorg'       : '{userinfoorg}',
            'userinfoserial'    : '{userinfoserial}',
            'username'          : cfg.user_name,
            'log'               : '',
        }

        if (inno := self.inno) is None or (inno.setup_info.Header.Flags & Flags.Uninstallable):
            map['uninstallexe'] = RF'{cfg.install_to}\unins000.exe'

        if cfg.x64:
            map['syswow64'] = R'C:\Windows\SysWOW64'
            map['commonpf32'] = R'C:\Program Files (x86)'
            map['commoncf32'] = R'C:\Program Files (x86)\Common Files'
            map['commonpf64'] = R'C:\Program Files'
            map['commoncf64'] = R'C:\Program Files\Common Files'
            map['dotnet2064'] = R'C:\Windows\Microsoft.NET\Framework64\v3.0'
            map['dotnet4064'] = R'C:\Windows\Microsoft.NET\Framework64\v4.0.30319'
        else:
            map['syswow64'] = R'C:\Windows\System32'
            map['commonpf32'] = R'C:\Program Files'
            map['commoncf32'] = R'C:\Program Files\Common Files'

        if cfg.windows_os_version[0] >= 10:
            map['userfonts'] = RF'{map["localappdata"]}\Microsoft\Windows\Fonts'

        if cfg.windows_os_version[0] >= 7:
            map['usercf'] = RF'{map["localappdata"]}\Programs\Common'
            map['userpf'] = RF'{map["localappdata"]}\Programs'

        for auto_var, admin_var, user_var in [
            ('autoappdata',       'commonappdata',       'userappdata',   ), # noqa
            ('autocf',            'commoncf',            'usercf',        ), # noqa
            ('autocf32',          'commoncf32',          'usercf',        ), # noqa
            ('autocf64',          'commoncf64',          'usercf',        ), # noqa
            ('autodesktop',       'commondesktop',       'userdesktop',   ), # noqa
            ('autodocs',          'commondocs',          'userdocs',      ), # noqa
            ('autofonts',         'commonfonts',         'userfonts',     ), # noqa
            ('autopf',            'commonpf',            'userpf',        ), # noqa
            ('autopf32',          'commonpf32',          'userpf',        ), # noqa
            ('autopf64',          'commonpf64',          'userpf',        ), # noqa
            ('autoprograms',      'commonprograms',      'userprograms',  ), # noqa
            ('autostartmenu',     'commonstartmenu',     'userstartmenu', ), # noqa
            ('autostartup',       'commonstartup',       'userstartup',   ), # noqa
            ('autotemplates',     'commontemplates',     'usertemplates', ), # noqa
        ]:
            try:
                map[auto_var] = map[admin_var] if cfg.admin else map[user_var]
            except KeyError:
                continue

        for legacy, new in [
            ('cf',     'commoncf',    ), # noqa
            ('cf32',   'commoncf32',  ), # noqa
            ('cf64',   'commoncf64',  ), # noqa
            ('fonts',  'commonfonts', ), # noqa
            ('pf',     'commonpf',    ), # noqa
            ('pf32',   'commonpf32',  ), # noqa
            ('pf64',   'commonpf64',  ), # noqa
            ('sendto', 'usersendto',  ), # noqa
        ]:
            try:
                map[legacy] = map[new]
            except KeyError:
                continue

        return map

    @external(static=False)
    def ExpandConstant(self, string: str) -> str:
        return self.expand_constant(string)

    @external(static=False)
    def ExpandConstantEx(self, string: str, custom_var: str, custom_val: str) -> str:
        return self.expand_constant(string, custom_var, custom_val)

    def expand_constant(
        self,
        string: str,
        custom_var: Optional[str] = None,
        custom_val: Optional[str] = None,
        unescape: bool = False
    ):
        config = self.config
        expand = partial(self.expand_constant, unescape=True)

        with io.StringIO() as result:
            constants = self.constant_map
            formatter = Formatter()
            backslash = False
            for prefix, spec, modifier, conversion in formatter.parse(string):
                if backslash and prefix[:1] == '\\':
                    prefix = prefix[1:]
                if unescape:
                    prefix = unquote(prefix)
                result.write(prefix)
                if spec is None:
                    continue
                elif spec == '\\':
                    if modifier or conversion:
                        raise IFPSException('Invalid format string.', ValueError(string))
                    value = spec
                elif spec == custom_var:
                    value = custom_val
                elif spec.startswith('%'):
                    name, p, default = spec[1:].partition('|')
                    name = expand(name)
                    default = expand(default)
                    try:
                        value = config.environment[name]
                    except KeyError:
                        value = default if p else F'%{name}%'
                elif spec == 'drive':
                    value = self.ExtractFileDrive(expand(modifier))
                elif spec == 'ini':
                    # {ini:Filename,Section,Key|DefaultValue}
                    _, _, default = modifier.partition('|')
                    value = expand(default)
                elif spec == 'cm':
                    # {cm:LaunchProgram,Inno Setup}
                    # The example above translates to "Launch Inno Setup" if English is the active language.
                    name, _, args = modifier.partition(',')
                    value = self.CustomMessage(expand(name))
                elif spec == 'reg':
                    # {reg:HKXX\SubkeyName,ValueName|DefaultValue}
                    _, _, default = modifier.partition('|')
                    value = expand(default)
                elif spec == 'param':
                    # {param:ParamName|DefaultValue}
                    _, _, default = modifier.partition('|')
                    value = expand(default)
                else:
                    try:
                        value = constants[spec]
                    except KeyError as KE:
                        raise IFPSException(F'invalid format field {spec}', KE) from KE
                backslash = value.endswith('\\')
                result.write(value)
            return result.getvalue()

    @external
    def DeleteFile(path: str) -> bool:
        return True

    @external
    def FileExists(file_name: str) -> bool:
        return False

    @external
    def Log(log: str):
        ...

    @external
    def Inc(p: Variable[Variable[int]]):
        p.set(p.get() + 1)

    @external
    def Dec(p: Variable[Variable[int]]):
        p.set(p.get() - 1)

    @external
    def FindFirst(file_name: str, frec: Variable) -> bool:
        return False

    @external
    def Trunc(x: float) -> float:
        return math.trunc(x)

    @external
    def GetSpaceOnDisk(
        path: str,
        in_megabytes: bool,
        avail: Variable[int],
        space: Variable[int],
    ) -> bool:
        _a = 3_000_000
        _t = 5_000_000
        if not in_megabytes:
            _a *= 1000
            _t *= 1000
        avail.set(_a)
        space.set(_t)
        return True

    @external
    def GetSpaceOnDisk64(
        path: str,
        avail: int,
        space: int,
    ) -> bool:
        avail.set(3_000_000_000)
        space.set(5_000_000_000)
        return True

    @external
    def Exec(
        exe: str,
        cmd: str,
        cwd: str,
        show: int,
        wait: int,
        out: Variable[int],
    ) -> bool:
        out.set(0)
        return True

    @external
    def GetCmdTail() -> str:
        return ''

    @external
    def ParamCount() -> int:
        return 0

    @external
    def ParamStr(index: int) -> str:
        return ''

    @external
    def ActiveLanguage() -> str:
        return 'en'

    @external(static=False)
    def CustomMessage(self, msg_name: str) -> str:
        by_language = {}
        for msg in self.inno.setup_info.Messages:
            if msg.EncodedName == msg_name:
                lng = msg.Language.Name
                if lng == self.config.language:
                    return msg.Value
                by_language[lng] = msg.Value
        try:
            return by_language[0]
        except KeyError:
            pass
        try:
            return next(iter(by_language.values()))
        except StopIteration:
            raise IFPSException(F'Custom message with name {msg_name} not found.')

    @external
    def FmtMessage(fmt: str, args: list[str]) -> str:
        fmt = fmt.replace('{', '{{')
        fmt = fmt.replace('}', '}}')
        fmt = '%'.join(re.sub('%(\\d+)', '{\\1}', p) for p in fmt.split('%%'))
        return fmt.format(*args)

    @external
    def Format(fmt: str, args: list[str | int | float]) -> str:
        try:
            formatted = fmt % tuple(args)
        except Exception:
            raise IFPSException('invalid format')
        else:
            return formatted

    @external(static=False)
    def SetupMessage(self, id: int) -> str:
        try:
            return self.inno.setup_info.Messages[id].Value
        except (AttributeError, IndexError):
            return ''

    @external(static=False, alias=['Is64BitInstallMode', 'IsX64Compatible', 'IsX64OS'])
    def IsWin64(self) -> bool:
        return self.config.x64

    @external(static=False)
    def IsX86OS(self) -> bool:
        return not self.config.x64

    @external
    def RaiseException(msg: str):
        raise IFPSException(msg)

    @external(static=False)
    def ProcessorArchitecture(self) -> int:
        if self.config.x64:
            return TSetupProcessorArchitecture.paX64.value
        else:
            return TSetupProcessorArchitecture.paX86.value

    @external(static=False)
    def GetUserNameString(self) -> str:
        return self.config.user_name

    @external(static=False)
    def GetComputerNameString(self) -> str:
        return self.config.host_name

    @external(static=False)
    def GetUILanguage(self) -> str:
        return self.config.lcid

    @external
    def GetArrayLength(array: Variable) -> int:
        array = array.deref()
        return len(array)

    @external
    def SetArrayLength(array: Variable, n: int):
        a = array.deref()
        a.resize(n)

    @external(static=False)
    def WizardForm(self) -> object:
        return self

    @external
    def Unassigned() -> None:
        return None

    @external
    def Null() -> None:
        return None

    @external(static=False)
    def Set8087CW(self, cw: int):
        self.fpucw = FPUControl(cw)

    @external(static=False)
    def Get8087CW(self) -> int:
        return self.fpucw.value

    @external
    def GetDateTimeString(
        fmt: str,
        date_separator: str,
        time_separator: str,
    ) -> str:
        from datetime import datetime
        now = datetime.now()
        date_separator = date_separator.lstrip('\0')
        time_separator = time_separator.lstrip('\0')

        def dt(m: re.Match[str]):
            spec = m[1]
            ampm = m[2]
            if ampm:
                am, _, pm = ampm.partition('/')
                spec = spec.upper()
                suffix = now.strftime('%p').lower()
                suffix = {'am': am, 'pm': pm}[suffix]
            else:
                suffix = ''
            if spec == 'dddddd' or spec == 'ddddd':
                return now.date.isoformat()
            if spec == 't':
                return now.time().isoformat('minutes')
            if spec == 'tt':
                return now.time().isoformat('seconds')
            if spec == 'd':
                return str(now.day)
            if spec == 'm':
                return str(now.month)
            if spec == 'h':
                return str(now.hour)
            if spec == 'n':
                return str(now.minute)
            if spec == 's':
                return str(now.second)
            if spec == 'H':
                return now.strftime('%I').lstrip('0') + suffix
            if spec == '/':
                return date_separator or spec
            if spec == ':':
                return time_separator or spec
            return now.strftime({
                'dddd'  : '%A',
                'ddd'   : '%a',
                'dd'    : '%d',
                'mmmm'  : '%B',
                'mmm'   : '%b',
                'mm'    : '%m',
                'yyyy'  : '%Y',
                'yy'    : '%y',
                'hh'    : '%H',
                'HH'    : '%I' + suffix,
                'nn'    : '%M',
                'ss'    : '%S',
            }.get(spec, m[0]))

        split = re.split(F'({formats.string!s})', fmt)
        for k in range(0, len(split), 2):
            split[k] = re.sub('([dmyhnst]+)((?:[aA][mM]?/[pP][mM]?)?)', dt, split[k])
        for k in range(1, len(split), 2):
            split[k] = split[k][1:-1]
        return ''.join(split)

    @external
    def Chr(b: int) -> str:
        return chr(b)

    @external
    def Ord(c: str) -> int:
        return ord(c)

    @external
    def Copy(string: str, index: int, count: int) -> str:
        index -= 1
        return string[index:index + count]

    @external
    def Length(string: str) -> int:
        return len(string)

    @external(alias='AnsiLowercase')
    def Lowercase(string: str) -> str:
        return string.lower()

    @external(alias='AnsiUppercase')
    def Uppercase(string: str) -> str:
        return string.upper()

    @external
    def StringOfChar(c: str, count: int) -> str:
        return c * count

    @external
    def Delete(string: Variable[str], index: int, count: int):
        index -= 1
        old = string.get()
        string.set(old[:index] + old[index + count:])

    @external
    def Insert(string: str, dest: Variable[str], index: int):
        index -= 1
        old = dest.get()
        dest.set(old[:index] + string + old[index:])

    @external(static=False)
    def StringChange(self, string: Variable[str], old: str, new: str) -> int:
        return self.StringChangeEx(string, old, new, False)

    @external
    def StringChangeEx(string: Variable[str], old: str, new: str, _: bool) -> int:
        haystack = string.get()
        count = haystack.count(old)
        string.set(haystack.replace(old, new))
        return count

    @external
    def Pos(string: str, sub: str) -> int:
        return string.find(sub) + 1

    @external
    def AddQuotes(string: str) -> str:
        if string and (string[0] != '"' or string[~0] != '"') and ' ' in string:
            string = F'"{string}"'
        return string

    @external
    def RemoveQuotes(string: str) -> str:
        if string and string[0] == '"' and string[~0] == '"':
            string = string[1:-1]
        return string

    @external(static=False)
    def CompareText(self, a: str, b: str) -> int:
        return self.CompareStr(a.casefold(), b.casefold())

    @external
    def CompareStr(a: str, b: str) -> int:
        if a > b:
            return +1
        if a < b:
            return -1
        return 0

    @external
    def SameText(a: str, b: str) -> bool:
        return a.casefold() == b.casefold()

    @external
    def SameStr(a: str, b: str) -> bool:
        return a == b

    @external
    def IsWildcard(pattern: str) -> bool:
        return '*' in pattern or '?' in pattern

    @external
    def WildcardMatch(text: str, pattern: str) -> bool:
        return fnmatch.fnmatch(text, pattern)

    @external
    def Trim(string: str) -> str:
        return string.strip()

    @external
    def TrimLeft(string: str) -> str:
        return string.lstrip()

    @external
    def TrimRight(string: str) -> str:
        return string.rstrip()

    @external
    def StringJoin(sep: str, values: list[str]) -> str:
        return sep.join(values)

    @external
    def StringSplitEx(string: str, separators: list[str], quote: str, how: TSplitType) -> list[str]:
        if not quote:
            parts = [string]
        else:
            quote = re.escape(quote)
            parts = re.split(F'({quote}.*?{quote})', string)
        sep = '|'.join(re.escape(s) for s in separators)
        out = []
        if how == TSplitType.stExcludeEmpty:
            sep = F'(?:{sep})+'
        for k in range(0, len(parts)):
            if k & 1 == 1:
                out.append(parts[k])
                continue
            out.extend(re.split(sep, string))
        if how == TSplitType.stExcludeLastEmpty:
            for k in reversed(range(len(out))):
                if not out[k]:
                    out.pop(k)
                    break
        return out

    @external(static=False)
    def StringSplit(self, string: str, separators: list[str], how: TSplitType) -> list[str]:
        return self.StringSplitEx(string, separators, None, how)

    @external(alias='StrToInt64')
    def StrToInt(s: str) -> int:
        return int(s)

    @external(alias='StrToInt64Def')
    def StrToIntDef(s: str, d: int) -> int:
        try:
            return int(s)
        except Exception:
            return d

    @external
    def StrToFloat(s: str) -> float:
        return float(s)

    @external(alias='FloatToStr')
    def IntToStr(i: int) -> str:
        return str(i)

    @external
    def StrToVersion(s: str, v: Variable[int]) -> bool:
        try:
            packed = bytes(map(int, s.split('.')))
        except Exception:
            return False
        if len(packed) != 4:
            return False
        v.set(int.from_bytes(packed, 'little'))
        return True

    @external
    def CharLength(string: str, index: int) -> int:
        return 1

    @external
    def AddBackslash(string: str) -> str:
        if string and string[~0] != '\\':
            string = F'{string}\\'
        return string

    @external
    def AddPeriod(string: str) -> str:
        if string and string[~0] != '.':
            string = F'{string}.'
        return string

    @external(static=False)
    def RemoveBackslashUnlessRoot(self, string: str) -> str:
        path = Path(string)
        if len(path.parts) == 1:
            return str(path)
        return self.RemoveBackslash(string)

    @external
    def RemoveBackslash(string: str) -> str:
        return string.rstrip('\\/')

    @external
    def ChangeFileExt(name: str, ext: str) -> str:
        if not ext.startswith('.'):
            ext = F'.{ext}'
        return str(Path(name).with_suffix(ext))

    @external
    def ExtractFileExt(name: str) -> str:
        return Path(name).suffix

    @external(alias='ExtractFilePath')
    def ExtractFileDir(name: str) -> str:
        dirname = str(Path(name).parent)
        return '' if dirname == '.' else dirname

    @external
    def ExtractFileName(name: str) -> str:
        if name:
            name = Path(name).parts[-1]
        return name

    @external
    def ExtractFileDrive(name: str) -> str:
        if name:
            parts = Path(name).parts
            if len(parts) >= 2 and parts[0] == '\\' and parts[1] == '?':
                parts = parts[2:]
            if parts[0] == '\\':
                if len(parts) >= 3:
                    return '\\'.join(parts[:3])
            else:
                root = parts[0]
                if len(root) == 2 and root[1] == ':':
                    return root
        return ''

    @external
    def ExtractRelativePath(base: str, dst: str) -> str:
        return str(Path(dst).relative_to(base))

    @external(static=False, alias='ExpandUNCFileName')
    def ExpandFileName(self, name: str) -> str:
        if self.ExtractFileDrive(name):
            return name
        return str(self.config.cwd / name)

    @external
    def SetLength(string: Variable[str], size: int):
        old = string.get()
        old = old.ljust(size, '\0')
        string.set(old[:size])

    @external(alias='OemToCharBuff')
    def CharToOemBuff(string: str) -> str:
        # TODO
        return string

    @external
    def Utf8Encode(string: str) -> str:
        return string.encode('utf8').decode('latin1')

    @external
    def Utf8Decode(string: str) -> str:
        return string.encode('latin1').decode('utf8')

    @external
    def GetMD5OfString(string: str) -> str:
        return hashlib.md5(string.encode('latin1')).hexdigest()

    @external
    def GetMD5OfUnicodeString(string: str) -> str:
        return hashlib.md5(string.encode('utf8')).hexdigest()

    @external
    def GetSHA1OfString(string: str) -> str:
        return hashlib.sha1(string.encode('latin1')).hexdigest()

    @external
    def GetSHA1OfUnicodeString(string: str) -> str:
        return hashlib.sha1(string.encode('utf8')).hexdigest()

    @external
    def GetSHA256OfString(string: str) -> str:
        return hashlib.sha256(string.encode('latin1')).hexdigest()

    @external
    def GetSHA256OfUnicodeString(string: str) -> str:
        return hashlib.sha256(string.encode('utf8')).hexdigest()

    @external
    def SysErrorMessage(code: int) -> str:
        return F'[description for error {code:08X}]'

    @external
    def MinimizePathName(path: str, font: object, max_len: int) -> str:
        return path

    @external(static=False)
    def CheckForMutexes(self, mutexes: str) -> bool:
        return any(m in self.mutexes for m in mutexes.split(','))

    @external(static=False)
    def CreateMutex(self, name: str):
        self.mutexes.add(name)

    @external(static=False)
    def GetWinDir(self) -> str:
        return self.expand_constant('{win}')

    @external(static=False)
    def GetSystemDir(self) -> str:
        return self.expand_constant('{sys}')

    @external(static=False)
    def GetWindowsVersion(self) -> int:
        version = int.from_bytes(struct.pack('>BBH', *self.config.windows_os_version))
        return version

    @external(static=False)
    def GetWindowsVersionEx(self, tv: Variable[Union[int, bool]]):
        tv[0], tv[1], tv[2] = self.config.windows_os_version # noqa
        tv[3], tv[4]        = self.config.windows_sp_version # noqa
        tv[5], tv[6], tv[7] = True, 0, 0

    @external(static=False)
    def GetWindowsVersionString(self) -> str:
        return '{0}.{1:02d}.{2:04d}'.format(*self.config.windows_os_version)

    @external
    def CreateOleObject(name: str) -> OleObject:
        return OleObject(name)

    @external
    def GetActiveOleObject(name: str) -> OleObject:
        return OleObject(name)

    @external
    def IDispatchInvoke(ole: OleObject, prop_set: bool, name: str, value: Any) -> int:
        return 0

    @external
    def FindWindowByClassName(name: str) -> int:
        return 0

    @external(static=False)
    def WizardSilent(self) -> bool:
        return self.config.wizard_silent

    @external(static=False)
    def SizeOf(self, var: Variable) -> int:
        if var.pointer:
            return (self.config.x64 + 1) * 4
        if var.container:
            return sum(self.SizeOf(x) for x in var.data)
        return var.type.code.width

    del external


class InnoSetupEmulator(IFPSEmulator):

    def emulate_installation(self, password=''):

        class SetupDispatcher:

            InitializeSetup: Callable
            InitializeWizard: Callable
            CurStepChanged: Callable
            ShouldSkipPage: Callable
            CurPageChanged: Callable
            PrepareToInstall: Callable
            CheckPassword: Callable
            NextButtonClick: Callable
            DeinitializeSetup: Callable

            def __getattr__(_, name):
                return (lambda *a: self.emulate_function(pfn, *a)) if (
                    pfn := self.symbols.get(name)
                ) else (lambda *_: False)

        Setup = SetupDispatcher()

        Setup.InitializeSetup()
        Setup.InitializeWizard()
        Setup.CurStepChanged(TSetupStep.ssPreInstall)

        for page in PageID:

            if not Setup.ShouldSkipPage(page):
                Setup.CurPageChanged(page)
                if page == PageID.wpPreparing:
                    Setup.PrepareToInstall(False)
                if page == PageID.wpPassword:
                    Setup.CheckPassword(password)

            Setup.NextButtonClick(page)

            if page == PageID.wpPreparing:
                Setup.CurStepChanged(TSetupStep.ssInstall)
            if page == PageID.wpInfoAfter:
                Setup.CurStepChanged(TSetupStep.ssPostInstall)

        Setup.CurStepChanged(TSetupStep.ssDone)
        Setup.DeinitializeSetup()

    def unimplemented(self, function: Function):
        decl = function.decl
        if decl is None:
            return
        if not decl.void:
            rc = 1
            rv = self.stack[-1]
            if not rv.container:
                rt = rv.type.py_type()
                if isinstance(rt, type) and issubclass(rt, int):
                    rv.set(1)
        else:
            rc = 0
        for k in range(rc, rc + len(decl.parameters)):
            ptr: Variable[Variable] = self.stack[-k]
            if not ptr.pointer:
                continue
            var = ptr.deref()
            if var.container:
                continue
            vt = var.type.py_type()
            if isinstance(vt, type) and issubclass(vt, int):
                var.set(1)
