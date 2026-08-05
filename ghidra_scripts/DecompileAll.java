import java.io.FileWriter;
import java.io.PrintWriter;
import java.util.ArrayList;
import java.util.List;

import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.Data;
import ghidra.program.model.listing.DataIterator;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionManager;
import ghidra.program.model.symbol.Symbol;
import ghidra.program.model.symbol.SymbolIterator;

public class DecompileAll extends GhidraScript {

    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        String cFile = args.length > 0 ? args[0] : "decompiled.c";
        String metaFile = args.length > 1 ? args[1] : "info.txt";

        PrintWriter meta = new PrintWriter(new FileWriter(metaFile));
        meta.println("=== FILE INFO ===");
        meta.println("File       : " + currentProgram.getName());
        meta.println("Language   : " + currentProgram.getLanguage().getLanguageID());
        meta.println("Compiler   : " + currentProgram.getCompilerSpec().getCompilerSpecID());
        meta.println("Processor  : " + currentProgram.getLanguage().getProcessor());
        meta.println("");

        List<String> strings = new ArrayList<>();
        DataIterator dataIt = currentProgram.getListing().getDefinedData(true);
        while (dataIt.hasNext() && strings.size() < 20000) {
            Data d = dataIt.next();
            if (d.hasStringValue() && d.getValue() instanceof String) {
                String s = (String) d.getValue();
                if (s.length() >= 4) {
                    strings.add(s);
                }
            }
        }
        meta.println("=== STRINGS (" + strings.size() + ") ===");
        for (String s : strings) {
            meta.println(s.replace("\n", "\\n").replace("\r", "\\r"));
        }
        meta.println("");

        List<String> symbols = new ArrayList<>();
        SymbolIterator it = currentProgram.getSymbolTable().getAllSymbols(true);
        while (it.hasNext() && symbols.size() < 20000) {
            Symbol s = it.next();
            symbols.add(s.getAddress() + "  " + s.getName());
        }
        meta.println("=== SYMBOLS (" + symbols.size() + ") ===");
        for (String s : symbols) {
            meta.println(s);
        }
        meta.close();

        DecompInterface decomp = new DecompInterface();
        decomp.openProgram(currentProgram);

        FunctionManager fm = currentProgram.getFunctionManager();
        List<Function> funcs = new ArrayList<>();
        for (Function f : fm.getFunctions(true)) {
            funcs.add(f);
        }
        funcs.sort((a, b) -> a.getEntryPoint().compareTo(b.getEntryPoint()));

        PrintWriter out = new PrintWriter(new FileWriter(cFile));
        out.println("/*");
        out.println(" * Ghidra decompiled output");
        out.println(" * File: " + currentProgram.getName());
        out.println(" * Functions: " + funcs.size());
        out.println(" */");
        out.println("");

        int total = funcs.size();
        int done = 0;
        int lastPrinted = -1;
        for (Function f : funcs) {
            out.println("// ---------- " + f.getName() + " @ " + f.getEntryPoint() + " ----------");
            DecompileResults res = decomp.decompileFunction(f, 120, null);
            if (res != null && res.decompileCompleted()) {
                out.println(res.getDecompiledFunction().getC());
            } else {
                out.println("/* [FAILED] could not decompile " + f.getName() + " */");
            }
            done++;
            int pct = (total == 0) ? 100 : (done * 100) / total;
            if (pct != lastPrinted && pct % 5 == 0) {
                println("DECOMP_PROGRESS " + done + "/" + total);
                lastPrinted = pct;
            }
        }
        out.close();
        decomp.dispose();

        println("DONE: wrote " + cFile + " with " + funcs.size() + " functions");
    }
}
