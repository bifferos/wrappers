"""
	This script repeatedly compiles snippets of code and interprets the results to generate Python modules
	containing definitions equivalent to the #defines found in a header file.
	
	It does this in the following steps:
		
		1)	Get some defines to try in the first place with a lazy parse of a header file.  They could alternatively
			come from an MSDN webscrape, which would be nicer, although sometimes MSDN documentation returns defines
			for a number of headers, rather than just one.
		2)	Compile each define in a program including the header in question.  The program uses the definition in 
			such a way that the compilation is guaranteed to fail due to type mismatch.  The real type will then get 
			printed in the warnings.
		3)	Next, create a function in a DLL, returning the define as the type obtained in 2).  This will give us the
			actual run-time value.
		4)	Finally, call the function from ctypes, setting restype as appropriate, to get the value.  Now we have the 
			type and the value, we can generate the wrapper code.
	

	Pros:
		=>	Very little code to write to generate wrappers.
		=>	Internal compiler definitions are taken into account without any extra work.
		=>	You don't need to understand all aspects of the C syntax, only what the compiler interprets from
			an identifier.  Particularly nice for defines that are derived from complex logical operations!
		
	Cons:
		=>	May fail for newer compilers that change error format (although Microsoft have been fairly consistent
			about that in the past).  Mitigating, you only need this to work for one compiler, and one set of 
			header files.  Few defs are introduced with new compiler releases, and it's often a long time before
			you *need* to use new windows functionality in a wrapper.
		=>	Slow in execution in comparison to writing your own preprocessor: lots of compilations.  Not as slow as 
			you might expect
		

	
"""


import subprocess, os, sys, re
from distutils.errors import CompileError, LinkError
from distutils import ccompiler

from ctypes import CDLL, windll
import ctypes


# The main program, that gets compiled to extract type information
g_main_test = r"""#include <windows.h>
#include <tchar.h>
#include <%(HDR)s>
typedef struct {
	DWORD dwDummy;
} %(ID)s;
void T%(ID)s(%(ID)s& tmp) {}
int main(){
#include "test.inc"
}"""

# The DLL that gets compiled to return the values
g_dll = """#include <windows.h>
#include <tchar.h>
#include <%(HDR)s>
extern "C"
{
#include "defines.inc"
}
"""


# Some exceptions to bail out of some edge-cases.

class TooManyErrors(Exception):
	"Too many errors, need to split into smaller compilation blocks to capture type info"
	pass

class NoCompile(Exception):
	"""The header does not compile in a trivial program, possibly shouldn't be used alone.
		You can hack the includes in the strings above and try again"""
	pass

class UnicodeOnly(Exception):
	"""The file hasn't been consistent in its TCHAR handling, so looks like it can only be 
		compiled unicode.  Definitions are normally captured from headers compiled _MBCS.  
		You could set the unicode define, or I could fix this by always assuming unicode, but
		that may bring other problems"""
	pass

class UnhandledDefine(Exception):
	"""There was a #define that we don't recognise.  Further processing or other techniques are
		required to extract it.
	"""
	pass



class RedirectedOutput:
	"""Python distutils compilation sends stuff to stdout, and this can't be easily stopped, so we
		redirect output to a file with a 'with' for the duration of the compilation.
	"""
	def __init__(self, name):
		self.name = name
	def __enter__(self):
		self.fp = file(self.name,"w")
		self.old = os.dup(sys.stdout.fileno())
		os.dup2(self.fp.fileno(), sys.stdout.fileno())
	def __exit__(self, *args):
		os.dup2(self.old, sys.stdout.fileno())
		self.fp.close()


def DropFile(txt, fname):
	"Drop a file to disk"
	lines = txt.split("\n")
	txt = "\r\n".join(lines)
	file(fname, "wb").write(txt)



class Defines:
	# To avoid any nasty surprisess generate a string guaranteed to be unique when generating any code.
	# The name of the #define identifier can be appended, giving a totally unique name in the 
	# compilation target that cannot clash with anything windows has to offer.  We can then match any
	# compiler warnings with the case we are looking for.
	ident = "__BB5ED02E_4203_438e_A71A_29F6BADE3FA7_very_unique"

	def __init__(self, header):
		self.header = header
		self.replacements = {"HDR":self.header,"ID":Defines.ident}
		self.GetDefinitions()
		self.cc = ccompiler.new_compiler()
		self.degs = []
		
		# These error constructs are checked early to ensure the later (per define) warning parser code
		# doesn't get confused.  
		
		# This results in an abort of the compilation.  We then re-run compilation of the individual defines
		self.re_fatal = re.compile(r"fatal error C1003: error count exceeds 100; stopping compilation")
		# We could probably re-try with unicode compilation for these, or switch this entire program to 
		# only compile unicode.
		self.re_unicode = re.compile(r".+ error C2308: concatenating mismatched strings.*")


	def GetPathToHeader(self):
		"Find full path to the requested header, searching through all INCLUDE directories"
		from distutils import msvc9compiler as mscompiler   # could try some other compilers for other python versions.
		includes = mscompiler.query_vcvarsall(mscompiler.get_build_version(), "x86")["include"].split(";")
		for path in includes:
			p = os.path.join(path, self.header).replace("\\\\", "\\")
			if os.path.isfile(p): return p
		raise ValueError("Can't find header")

	def GetDefinitions(self):
		"""Crude pre-parse of header file to get candidate definitions.  This list will then be whittled 
			down by removing some cases that we don't handle, and then at the compilation stage we may
			decide we don't like the compiler output and remove some more.  It's just a starting point.
		"""
		p = self.GetPathToHeader()
		rex = re.compile(r"\s*#\s*define\s+([a-zA-Z_][0-9a-zA-Z_]*)\s+(\S+)")
		self.defs = []
		found = {}
		for line in file(p,"rb").readlines():
			m = rex.match(line)
			if m:
				name = m.groups()[0]
				if name.startswith("IID_"): continue   # don't handle this yet.
				if name.startswith("__"): continue   # do we need these?
				if not name in found:
					self.defs.append(name)
					found[name] = None

	def CheckWarningsForBail(self, lines):
		"""Early check of the entire list of warnings generated from the compile to see if there's anything
			we can't handle.  Much simpler than dealing with it in the lower levels"""
		for line in lines:
			if self.re_fatal.match(line):
				raise TooManyErrors("Too many errors")
			if self.re_unicode.match(line):
				raise UnicodeOnly("Probably only compiles unicode (not ansi)")
			

	def GroupWarningsByLine(self, lines, incfile):
		"""As title, we need to associate generated warnings with a line.  Some warnings conveniently have 
			a line number associated with our generated file, but some will reference header lines, or 
			possibly not reference any files at all."""
		incfile = incfile.lower().replace("\\", "\\\\")
		#print "incfile:", incfile
		rex = re.compile(incfile+r"\((\d+)\) \: (.+)")
		out = {}
		lastline = 1
		for line in lines:
			m = rex.match(line)
			if m:
				ln, err = m.groups()
				num = int(ln)
			else:
				num = lastline
				err = line
			if num in out:
				out[num].append(err)
			else:
				out[num] = [err]
			lastline = num
		return out

	def RecoverTypeInfo(self, warnings):
		"""This does most of the work.   An attempt is made here to be specific about the kinds of errors we are 
			looking for, so we can examine non-matching cases to see if they could be of interest
		"""
		decorator = re.compile(r"error C2660: 'T%(ID)s' : function does not take 0 arguments" % self.replacements)    # __stdcall?
		multi = re.compile(r"error C2660: 'T%(ID)s' : function does not take \d+ arguments" % self.replacements)    # multiple values
		undeclared = re.compile(r"error C2065: '.+' : undeclared identifier")
		illegal = re.compile(r"error C2275: '.+' : illegal use of this type as an expression")
		suffix = re.compile(r"error C\d+: syntax error : .+")
		intrinsic = re.compile(r"error C\d+: '.+' : bad context for intrinsic function.*")
		conversion = re.compile(r"error C2664: 'T%(ID)s' : cannot convert parameter 1 from '(.+)' to '%(ID)s &'" % self.replacements)
		# No type probably means an enum
		enum = re.compile(r"error C2664: 'T%(ID)s' : cannot convert parameter 1 from '' to '%(ID)s &'" % self.replacements)

		for line in warnings:
			for r in [decorator, undeclared, illegal, suffix, multi, intrinsic, enum]:
				if r.match(line): return None

		for line in warnings:
			m = conversion.match(line)
			if m:
				return m.groups()[0]

		self.warnings = warnings

		raise UnhandledDefine("A dodgy struct of some sort, perhaps a function pointer or something else.")


	def ParseCompilerOutput(self, txt, incfile):
		"The high-level steps in parsing output of main.cpp"
		lines = [i for i in txt.split("\n")[1:] if i.strip()]   # skip over the name of the source file

		self.CheckWarningsForBail(lines)

		groups = self.GroupWarningsByLine(lines, incfile)
		sorted = groups.keys()
		sorted.sort()
		return [(num, self.RecoverTypeInfo(groups[num])) for num in sorted]

	def TestCompile(self, txt):
		"""Perform a compilation.  And return type information.  txt is the include file the bulk of the 
			compilation unit."""
		incfile = "test.inc"
		DropFile(g_main_test % self.replacements, "Main.cpp")
		DropFile(txt, incfile)
		ok = True
		rd = RedirectedOutput("errors.txt")
		with rd:
			try:
				self.cc.compile(["main.cpp"])
			except CompileError:
				ok = False
		out = file(rd.name,"r").read()
		return self.ParseCompilerOutput(out, os.path.abspath(incfile))
    
	def CompileBatch(self, batch):
		"Batches up a series of defines into a single compilation unit"
		lines = ["T%s(%s);" %(Defines.ident, define) for define in batch]
		return self.TestCompile("\n".join(lines))

	def GetDefineTypes(self):
		"""Return the type information for the defines.  Compilation occurs in blocks of 80 defines
			to give some leeway for some of them to generate more than one error.  The compiler limit
			is 100 errors which is hard-coded.  In the event that 80 defines generate more than 100 
			errors, switch to compiling them one-by-one.  This will take a *lot* longer, but is the
			safest bet.
		"""
		n = 80
		batches = [self.defs[i:i+n] for i in xrange(0, len(self.defs), n)]
		total = []
		for batch in batches:
			try:
				batch_result = self.CompileBatch(batch)
			except TooManyErrors:
				batch_result = [self.CompileBatch([define])[0] for define in batch]   # one at a time
			# combine with definitions
			types = zip(batch, [j for i,j in batch_result])
			types = [list(i) for i in types]   # don't want tuples
			total += types
			
		return total
		
	def CompileRunDll(self, defs):
		"""Compile up a dll which has an entry point per define.  The entry point names are mangled with the unique
			string to ensure they are unique.  The dll is called with the ctypes return type set to the type previously 
			discovered in the compilation step.  A table determines the mapping between the C type and the ctypes type.
			Some reinterpretation is done based on the value of string pointers.  Some #defines are actually numbers
			cast to strings and this must be handled.
		"""
		DropFile(g_dll % self.replacements, "dll.cpp")
		fp = file("defines.inc","w")
		index = []
		for n,t in defs:
			func = "F%s_%s"%(self.replacements["ID"],n)
			fp.write("__declspec(dllexport) %s %s(const char* n) { return %s; }\n" % (t, func, n))
			index.append((n,t,func))
		fp.close()

		self.cc.compile(["dll.cpp"])

		modname = "dll.dll"
		if os.path.isfile(modname):
			os.unlink(modname)

		ok = True
		rd = RedirectedOutput("errors.txt")
		with rd:
			try:
				self.cc.link("dll", ["dll"], modname)
			except LinkError:
				ok = False
		out = file(rd.name,"r").read()

		if not ok:
			print "Unable to link with these defines in a module"
			return []
			
		dll = CDLL(modname)

		type_table = {
			"double": ctypes.c_double,
			"const char *": ctypes.c_char_p,
			"const wchar_t *": ctypes.c_wchar_p,
			"int": ctypes.c_int,
			"unsigned int": ctypes.c_uint,
			"unsigned long": ctypes.c_ulong,
			"LPCSTR": ctypes.c_ulong,
		}

		ret = []
		for n,t,func in index: 
			fn = getattr(dll, func)
			if type_table.has_key(t):
				fn.restype = type_table[t]

			val = fn()
			if hasattr(val,"value"):
				val_str = repr(val.value)
			else:
				val_str = repr(val)

			if t == "LPCSTR":
				if val>0xffff:   
					val_str = '"'+c_char_p(val).value+'"'
	
			ret.append((n,val_str))
			
		handle = dll._handle
		windll.kernel32.FreeLibrary(handle)

		return ret


def Transform(defs):
	"""Some types are either not suitable (function definitions), or need some slight adjustments (char arrays instead
		of char pointers).  Either remove them from processing, or adjust the types.
	"""
	
	xform = [
		(re.compile(r"const wchar_t \[\d+\]"), "const wchar_t *"),
		(re.compile(r"const char \[\d+\]"), "const char *"),
		(re.compile(r".+\(__stdcall \*\)\(.+\)"), None),
		(re.compile(r".+\(__cdecl \*\)\(.+\)"), None),
		(re.compile(r"overloaded-function"), None),
		]
	for i in xrange(0,len(defs)):
		n, t = defs[i]
		if not t: continue
		for rex, subs in xform:
			if rex.match(t):
				defs[i][1] = subs
				break


def GenerateModule(hdrname, modname):
	"""Actually generate the python module that defines the values
		This could alternatively generate a C python extension.
	"""
	print "Generating module from", hdrname
	defs = Defines(hdrname)

	ok = False
	try:
		defs.TestCompile("")
		ok = True
	except NoCompile:
		pass
	except UnhandledDefine:
		pass

	if not ok:
		print "Can't compile with this header, probably needs something else including first."
		return


	# Run lots of compilations, pick up defines.
	try:
		total = defs.GetDefineTypes()
	except UnhandledDefine:
		print defs.warnings
		raise
	except UnicodeOnly:
		print "It seems this header can only be compile unicode for at least one define to work properly."
		return


	# lose values that are probably not of interest.
	Transform(total)   
	total = [(i,j) for i,j in total if j]
	
	# Create a dll of functions which return value as pointer.  This means either return an integer
	# or a string pointer.
	vals = defs.CompileRunDll(total)

	# Write out a module with the definitions.
	fp = file(modname, "w")
	for n,v in vals:
		fp.write("%s=%s\n" % (n, v))

	fp.close()
	print "%s written, %d defines." % (modname, len(vals))



def RunTest():
	"""A little test to process all headers in the SDK, and make sure we at least bail gracefully if we 
		don't manage to deal with them.  There is a bug somewhere that leads to the compiler (or distutils?) 
		complaining about too many open files if we do them all in one go, so I've split them up into two lots.
		
	"""
	inc = r"C:\Program Files\Microsoft SDKs\Windows\v6.0A\Include"
	headers = os.listdir(inc)
	
	to_process = "abcdefghijklmn"   # do headers starting [a-n]
	#to_process = "opqrstuvwxyz"     # do headers starting [o-z]
	
	for h in headers:
		if not h.lower()[0] in to_process : continue
		p = os.path.join(inc, h)
		if os.path.isfile(p) and p.endswith(".h"):
			GenerateModule(h, "out.py")


if __name__ == "__main__":
	GenerateModule("wincrypt.h","wincrypt.py")
	#RunTest()








    