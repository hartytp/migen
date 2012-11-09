import inspect
import ast
from operator import itemgetter

from migen.fhdl.structure import *
from migen.fhdl import visit as fhdl
from migen.corelogic.fsm import FSM
from migen.pytholite import transel

class FinalizeError(Exception):
	pass

class _AbstractLoad:
	def __init__(self, target, source):
		self.target = target
		self.source = source
	
	def lower(self):
		if not self.target.finalized:
			raise FinalizeError
		return self.target.sel.eq(self.target.source_encoding[self.source])

class _LowerAbstractLoad(fhdl.NodeTransformer):
	def visit_unknown(self, node):
		if isinstance(node, _AbstractLoad):
			return node.lower()
		else:
			return node

class _Register:
	def __init__(self, name, nbits):
		self.storage = Signal(BV(nbits), name=name)
		self.source_encoding = {}
		self.finalized = False
	
	def load(self, source):
		if source not in self.source_encoding:
			self.source_encoding[source] = len(self.source_encoding) + 1
		return _AbstractLoad(self, source)
	
	def finalize(self):
		if self.finalized:
			raise FinalizeError
		self.sel = Signal(BV(bits_for(len(self.source_encoding) + 1)), name="pl_regsel")
		self.finalized = True
	
	def get_fragment(self):
		if not self.finalized:
			raise FinalizeError
		# do nothing when sel == 0
		items = sorted(self.source_encoding.items(), key=itemgetter(1))
		cases = [(Constant(v, self.sel.bv),
			self.storage.eq(k)) for k, v in items]
		sync = [Case(self.sel, *cases)]
		return Fragment(sync=sync)

class _Compiler:
	def __init__(self, symdict, registers):
		self.symdict = symdict
		self.registers = registers
		self.targetname = ""
	
	def visit_top(self, node):
		if isinstance(node, ast.Module) \
		  and len(node.body) == 1 \
		  and isinstance(node.body[0], ast.FunctionDef):
			return self.visit_block(node.body[0].body)
		else:
			raise NotImplementedError
	
	# blocks and statements
	def visit_block(self, statements):
		states = []
		for statement in statements:
			if isinstance(statement, ast.Assign):
				op = self.visit_assign(statement)
				if op:
					states.append(op)
			else:
				raise NotImplementedError
		return states
	
	def visit_assign(self, node):
		if isinstance(node.targets[0], ast.Name):
			self.targetname = node.targets[0].id
		value = self.visit_expr(node.value, True)
		self.targetname = ""
		
		if isinstance(value, _Register):
			self.registers.append(value)
			for target in node.targets:
				if isinstance(target, ast.Name):
					self.symdict[target.id] = value
				else:
					raise NotImplementedError
			return []
		elif isinstance(value, Value):
			r = []
			for target in node.targets:
				if isinstance(target, ast.Attribute) and target.attr == "store":
					treg = target.value
					if isinstance(treg, ast.Name):
						r.append(self.symdict[treg.id].load(value))
					else:
						raise NotImplementedError
				else:
					raise NotImplementedError
			return r
		else:
			raise NotImplementedError
	
	# expressions
	def visit_expr(self, node, allow_call=False):
		if isinstance(node, ast.Call):
			if allow_call:
				return self.visit_expr_call(node)
			else:
				raise NotImplementedError
		elif isinstance(node, ast.BinOp):
			return self.visit_expr_binop(node)
		elif isinstance(node, ast.Compare):
			return self.visit_expr_compare(node)
		elif isinstance(node, ast.Name):
			return self.visit_expr_name(node)
		elif isinstance(node, ast.Num):
			return self.visit_expr_num(node)
		else:
			raise NotImplementedError
	
	def visit_expr_call(self, node):
		if isinstance(node.func, ast.Name):
			callee = self.symdict[node.func.id]
		else:
			raise NotImplementedError
		if callee == transel.Register:
			if len(node.args) != 1:
				raise TypeError("Register() takes exactly 1 argument")
			nbits = ast.literal_eval(node.args[0])
			return _Register(self.targetname, nbits)
		else:
			raise NotImplementedError
	
	def visit_expr_binop(self, node):
		left = self.visit_expr(node.left)
		right = self.visit_expr(node.right)
		if isinstance(node.op, ast.Add):
			return left + right
		elif isinstance(node.op, ast.Sub):
			return left - right
		elif isinstance(node.op, ast.Mult):
			return left * right
		elif isinstance(node.op, ast.LShift):
			return left << right
		elif isinstance(node.op, ast.RShift):
			return left >> right
		elif isinstance(node.op, ast.BitOr):
			return left | right
		elif isinstance(node.op, ast.BitXor):
			return left ^ right
		elif isinstance(node.op, ast.BitAnd):
			return left & right
		else:
			raise NotImplementedError
	
	def visit_expr_compare(self, node):
		test = visit_expr(node.test)
		r = None
		for op, rcomparator in zip(node.ops, node.comparators):
			comparator = visit_expr(rcomparator)
			if isinstance(op, ast.Eq):
				comparison = test == comparator
			elif isinstance(op, ast.NotEq):
				comparison = test != comparator
			elif isinstance(op, ast.Lt):
				comparison = test < comparator
			elif isinstance(op, ast.LtE):
				comparison = test <= comparator
			elif isinstance(op, ast.Gt):
				comparison = test > comparator
			elif isinstance(op, ast.GtE):
				comparison = test >= comparator
			else:
				raise NotImplementedError
			if r is None:
				r = comparison
			else:
				r = r & comparison
			test = comparator
		return r
	
	def visit_expr_name(self, node):
		r = self.symdict[node.id]
		if isinstance(r, _Register):
			r = r.storage
		return r
	
	def visit_expr_num(self, node):
		return node.n

def _create_fsm(states):
	stnames = ["S" + str(i) for i in range(len(states))]
	fsm = FSM(*stnames)
	for i, state in enumerate(states):
		if i == len(states) - 1:
			actions = []
		else:
			actions = [fsm.next_state(getattr(fsm, stnames[i+1]))]
		actions += state
		fsm.act(getattr(fsm, stnames[i]), *actions)
	return fsm

def make_pytholite(func):
	tree = ast.parse(inspect.getsource(func))
	symdict = func.__globals__.copy()
	registers = []
	
	print("ast:")
	print(ast.dump(tree))
	
	states = _Compiler(symdict, registers).visit_top(tree)
	
	print("compilation result:")
	print(states)
	
	regf = Fragment()
	for register in registers:
		register.finalize()
		regf += register.get_fragment()
	
	fsm = _create_fsm(states)
	fsmf = _LowerAbstractLoad().visit(fsm.get_fragment())
	
	return regf + fsmf
