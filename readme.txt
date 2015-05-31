One of the problems with developing Windows Python code is the amount of work 
needed when you move away from the functions that the win32 extensions support.
This means resorting to Power Shell (bleh...), or multiple callouts to 
netsh (sloowwww), and/or scraping the output from ifconfig and numerous other 
windows utilities to get the job done.  I've seen people putting binary blocks 
of data into their scripts, destined for some area of the registry, simply 
because the APIs (if you're not a C programmer, and sometimes even if you are) 
are just too difficult to grok, let alone shoe-horn into your latest Python 
masterpiece.

I'm not saying this solves all (or even any) of the problems, but it's an area 
that interests me, and it led me to looking at other approaches to easing use 
of the win32 API from python.  
	
Any C/C++ programmer after years of developing for Windows, starts to see 
patterns (as well as gross inconsistencies) in the windows API.  Although it's
normous, there are only so many techniques that can be used to talk to a C API,
and I've always felt that if those specific cases were dealt with, it would be 
possible to generate wrappers for high level languages without resorting to 
Gcc-xml, SWIG or any of the other wrapper generators.  They all save you work, 
however there is still an enormous amount of work to do (even once you've 
understood them).
	
I came up with an idea, that since developing C programs is almost impossible 
without a compiler (and possibly a debugger as well), why not use the compiler,
as an inexperienced programmer might, as a way of generating wrappers.  This is
the first part of that implementation.  The next parts, generation of structs,
and function wrappers themselves I hope to have time to do afterwards.  I already
have a hacked-up proof of concept for structs, so watch this space :-).

