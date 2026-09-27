@echo off
rem Tournament date fix for Emerald V8.04 (MegaPPBox), sent by the
rem TournaMAXX server as MEGACDLL.NEW.  Run once by TEST.BAT at boot.
if not exist c:\merit2\exec\megacdll.new goto nonew
dir c:\merit2\exec\megacdll.exe > c:\tmfix.dir
find "1,713,893" c:\tmfix.dir > nul
if not errorlevel 1 goto oldok
find "1713893" c:\tmfix.dir > nul
if errorlevel 1 goto wrong
:oldok
dir c:\merit2\exec\megacdll.new > c:\tmfix.dir
find "1,713,893" c:\tmfix.dir > nul
if not errorlevel 1 goto newok
find "1713893" c:\tmfix.dir > nul
if errorlevel 1 goto short
:newok
copy /b c:\merit2\exec\megacdll.new c:\merit2\exec\megacdll.exe > nul
if errorlevel 1 goto copyfail
del c:\merit2\exec\megacdll.new
if exist d:\database\tourney.bak del d:\database\tourney.bak
if exist d:\database\tourney.dbf del d:\database\tourney.dbf
echo TMFIX-804 INSTALLED> c:\tmfix.txt
goto end
:nonew
echo TMFIX-804 NO MEGACDLL.NEW> c:\tmfix.txt
goto end
:wrong
echo TMFIX-804 NOT EMERALD V8.04, NOTHING CHANGED> c:\tmfix.txt
del c:\merit2\exec\megacdll.new
goto end
:short
echo TMFIX-804 MEGACDLL.NEW INCOMPLETE, NOTHING CHANGED> c:\tmfix.txt
del c:\merit2\exec\megacdll.new
goto end
:copyfail
echo TMFIX-804 COPY FAILED> c:\tmfix.txt
:end
if exist c:\tmfix.dir del c:\tmfix.dir
