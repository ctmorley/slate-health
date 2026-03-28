/**
 * Schema Parity Test
 *
 * Verifies that frontend TypeScript types in src/types/index.ts define the same
 * interfaces (with matching field names AND compatible field types) as the backend
 * Pydantic schemas. This catches drift between the two layers when one side
 * adds/removes/renames a field or changes a field's type/nullability.
 *
 * Approach: We dynamically parse the backend Pydantic schema .py files to extract
 * class names, field names, and field types, then compare against the frontend TS
 * interfaces. This eliminates the hardcoded "expected fields" approach that can go
 * stale.
 *
 * Backend→Frontend name mapping: When the frontend intentionally uses a different
 * name for a backend schema (e.g. `AuditLogItem` → `AuditLogEntry`), the mapping
 * is declared in BACKEND_TO_FRONTEND_ALIASES below and enforced by the test.
 */
import { describe, it, expect, beforeAll } from "vitest";
import * as fs from "fs";
import * as path from "path";

// ---------------------------------------------------------------------------
// Lightweight TS interface parser (with types)
// ---------------------------------------------------------------------------

interface ParsedField {
  name: string;
  type: string;
  optional: boolean;
}

interface ParsedInterface {
  name: string;
  fields: string[];
  fieldTypes: Map<string, ParsedField>;
}

/**
 * Extract interface declarations, their field names, and field types from a
 * TypeScript source file. Handles `export interface Foo { ... }` blocks.
 */
function parseInterfaces(source: string): ParsedInterface[] {
  const results: ParsedInterface[] = [];
  const interfaceRe = /export\s+interface\s+(\w+)\s*\{/g;
  let match: RegExpExecArray | null;

  while ((match = interfaceRe.exec(source)) !== null) {
    const name = match[1];
    const startIdx = match.index + match[0].length;

    let depth = 1;
    let i = startIdx;
    while (i < source.length && depth > 0) {
      if (source[i] === "{") depth++;
      else if (source[i] === "}") depth--;
      i++;
    }
    const body = source.slice(startIdx, i - 1);

    const fields: string[] = [];
    const fieldTypes = new Map<string, ParsedField>();
    // Match field declarations with types, handling multi-token types and
    // JSDoc comments. Captures: name, optional marker, type annotation.
    const fieldRe = /^\s+(?:\/\*\*[^*]*\*\/\s*)?(\w+)(\?)?:\s*(.+?);\s*$/gm;
    let fm: RegExpExecArray | null;
    while ((fm = fieldRe.exec(body)) !== null) {
      const fieldName = fm[1];
      const optional = fm[2] === "?";
      const rawType = fm[3].trim();
      fields.push(fieldName);
      fieldTypes.set(fieldName, { name: fieldName, type: rawType, optional });
    }

    results.push({ name, fields, fieldTypes });
  }

  return results;
}

// ---------------------------------------------------------------------------
// Lightweight Pydantic model parser (with types)
// ---------------------------------------------------------------------------

interface ParsedPydanticField {
  name: string;
  type: string;
  nullable: boolean;
  hasDefault: boolean;
}

interface ParsedPydanticModel {
  name: string;
  fields: string[];
  fieldTypes: Map<string, ParsedPydanticField>;
}

/**
 * Extract Pydantic BaseModel subclass names, their field names, and field types
 * from a Python source file. Handles `class Foo(BaseModel):` blocks with typed
 * field declarations like `field_name: type = ...`.
 *
 * Limitations: does not resolve inheritance beyond BaseModel, ignores
 * @validator/@field_validator methods, and doesn't handle multi-line field
 * definitions that span more than one line. This is intentionally simple — it
 * covers the patterns used in our backend schemas.
 */
function parsePydanticModels(source: string): ParsedPydanticModel[] {
  const results: ParsedPydanticModel[] = [];

  // Split into lines for block detection
  const lines = source.split("\n");
  let i = 0;

  while (i < lines.length) {
    // Match `class Foo(BaseModel):` or `class Foo(BaseModel, ...):` patterns
    const classMatch = lines[i].match(
      /^class\s+(\w+)\s*\([^)]*BaseModel[^)]*\)\s*:/,
    );
    if (!classMatch) {
      i++;
      continue;
    }

    const className = classMatch[1];
    const fields: string[] = [];
    const fieldTypes = new Map<string, ParsedPydanticField>();
    i++; // Move past the class line

    // Parse the class body (indented lines)
    while (i < lines.length) {
      const line = lines[i];

      // Empty lines and comments are fine inside the class body
      if (line.trim() === "" || line.trim().startsWith("#")) {
        i++;
        continue;
      }

      // If line is not indented, we've left the class body
      if (line.length > 0 && !line.startsWith(" ") && !line.startsWith("\t")) {
        break;
      }

      const trimmed = line.trim();

      // Skip decorators, methods, docstrings, model_config, validators
      if (
        trimmed.startsWith("@") ||
        trimmed.startsWith("def ") ||
        trimmed.startsWith('"""') ||
        trimmed.startsWith("'''") ||
        trimmed.startsWith("model_config")
      ) {
        i++;
        // Skip multi-line docstrings
        if (trimmed.startsWith('"""') && !trimmed.endsWith('"""')) {
          i++;
          while (i < lines.length && !lines[i].trim().endsWith('"""')) i++;
          i++; // skip closing """
        }
        if (trimmed.startsWith("'''") && !trimmed.endsWith("'''")) {
          i++;
          while (i < lines.length && !lines[i].trim().endsWith("'''")) i++;
          i++;
        }
        continue;
      }

      // Match field declarations: `field_name: Type` or `field_name: Type = ...`
      const fieldMatch = trimmed.match(/^(\w+)\s*:\s*(.+?)(?:\s*=\s*(.+))?$/);
      if (fieldMatch) {
        const fieldName = fieldMatch[1];
        let rawType = fieldMatch[2].trim();
        const defaultPart = fieldMatch[3]?.trim();

        // Strip trailing Field(...) from type if = Field(...) was captured as part of type
        rawType = rawType.replace(/\s*=\s*Field\(.*$/, "").trim();

        const nullable = rawType.includes("| None") || rawType.includes("None |") || rawType.includes("Optional[");
        const hasDefault = defaultPart !== undefined;

        fields.push(fieldName);
        fieldTypes.set(fieldName, {
          name: fieldName,
          type: rawType,
          nullable,
          hasDefault,
        });
      }

      i++;
    }

    results.push({ name: className, fields, fieldTypes });
  }

  return results;
}

// ---------------------------------------------------------------------------
// Python → TypeScript type compatibility mapping
// ---------------------------------------------------------------------------

/**
 * Normalize a Python type annotation to its TypeScript equivalent category.
 * Returns a simplified canonical form that can be compared across languages.
 *
 * Categories:
 * - "string" — str, uuid.UUID, datetime (serialized as string in JSON)
 * - "number" — int, float
 * - "boolean" — bool
 * - "object" — dict[str, Any], Record<string, unknown>
 * - "array:T" — list[T] (element type normalized recursively)
 * - "enum:..." — literal/union string types
 * - null marker appended as "|null" for nullable types
 */
function normalizePythonType(pyType: string): string {
  // Remove leading/trailing whitespace
  let t = pyType.trim();

  // Handle Optional[X] → X | None
  const optionalMatch = t.match(/^Optional\[(.+)\]$/);
  if (optionalMatch) {
    t = optionalMatch[1] + " | None";
  }

  // Check nullability
  const isNullable = t.includes("| None") || t.includes("None |");
  // Strip None from union for base type analysis
  let base = t
    .replace(/\s*\|\s*None/g, "")
    .replace(/None\s*\|\s*/g, "")
    .trim();

  const result = normalizePythonBaseType(base);
  return isNullable ? result + "|null" : result;
}

function normalizePythonBaseType(base: string): string {
  // Atomic types
  if (base === "str") return "string";
  if (base === "uuid.UUID" || base === "UUID") return "string";
  if (base === "datetime" || base === "date") return "string";
  if (base === "int") return "number";
  if (base === "float") return "number";
  if (base === "bool") return "boolean";

  // dict / Dict → object
  if (base.match(/^dict\[/i) || base === "dict" || base.match(/^Dict\[/)) return "object";

  // list[T] / List[T]
  const listMatch = base.match(/^(?:list|List)\[(.+)\]$/);
  if (listMatch) {
    const inner = normalizePythonBaseType(listMatch[1].trim());
    return `array:${inner}`;
  }

  // Array bare
  if (base === "list" || base === "List") return "array:unknown";

  // Any
  if (base === "Any") return "unknown";

  // Fall back to raw string for complex/custom types
  return base;
}

function normalizeTsType(tsType: string): string {
  let t = tsType.trim();

  // Check nullability (TS uses `| null` and optional `?`)
  const isNullable = t.includes("| null") || t.includes("null |");
  let base = t
    .replace(/\s*\|\s*null/g, "")
    .replace(/null\s*\|\s*/g, "")
    .trim();

  // Remove undefined unions too (for optional fields)
  base = base
    .replace(/\s*\|\s*undefined/g, "")
    .replace(/undefined\s*\|\s*/g, "")
    .trim();

  const result = normalizeTsBaseType(base);
  return isNullable ? result + "|null" : result;
}

function normalizeTsBaseType(base: string): string {
  if (base === "string") return "string";
  if (base === "number") return "number";
  if (base === "boolean") return "boolean";

  // Record<string, unknown> → object
  if (base.match(/^Record</) || base === "object") return "object";

  // Array<T> or T[]
  const arrayGenericMatch = base.match(/^Array<(.+)>$/);
  if (arrayGenericMatch) {
    const inner = normalizeTsBaseType(arrayGenericMatch[1].trim());
    return `array:${inner}`;
  }
  const arraySuffixMatch = base.match(/^(.+)\[\]$/);
  if (arraySuffixMatch) {
    const inner = normalizeTsBaseType(arraySuffixMatch[1].trim());
    return `array:${inner}`;
  }

  // Common TS types
  if (base === "unknown" || base === "any") return "unknown";

  // Union of string literals (e.g., "saml" | "oidc") → string
  if (base.match(/^["'][^"']+["'](\s*\|\s*["'][^"']+["'])*$/)) return "string";

  // AgentType, TaskStatus etc. — branded string enums → string
  if (base === "AgentType" || base === "TaskStatus") return "string";

  // Inline object types like `{ date: string; count: number }`
  if (base.startsWith("{") && base.endsWith("}")) return "object";

  // Custom interfaces referenced as types (e.g., AgentStatsResponse, SchedulingSlot)
  // These are complex objects
  return base;
}

/**
 * Resolve a type name through the backend→frontend alias mapping.
 * If `name` is a backend name with a known frontend alias, returns the alias.
 * Also handles the reverse: if `name` is a frontend alias, returns the backend name.
 * This lets us compare `list[AuditLogItem]` with `AuditLogEntry[]` as equivalent.
 */
function resolveAlias(name: string): string {
  // Check if it's a backend name → return frontend name
  if (BACKEND_TO_FRONTEND_ALIASES[name]) return BACKEND_TO_FRONTEND_ALIASES[name];
  // Check reverse: frontend name → find if any backend name maps to it
  for (const [backend, frontend] of Object.entries(BACKEND_TO_FRONTEND_ALIASES)) {
    if (name === frontend) return backend;
  }
  return name;
}

/**
 * Check if two normalized types are compatible.
 * Returns true if they represent the same logical JSON type.
 */
function typesAreCompatible(pyNorm: string, tsNorm: string): boolean {
  // Exact match
  if (pyNorm === tsNorm) return true;

  // Separate nullability
  const pyNull = pyNorm.endsWith("|null");
  const tsNull = tsNorm.endsWith("|null");
  const pyBase = pyNorm.replace(/\|null$/, "");
  const tsBase = tsNorm.replace(/\|null$/, "");

  // Base types must match (with alias resolution for custom types)
  if (pyBase !== tsBase) {
    // For arrays, check inner type compatibility
    if (pyBase.startsWith("array:") && tsBase.startsWith("array:")) {
      const pyInner = pyBase.slice(6);
      const tsInner = tsBase.slice(6);
      // Check with alias resolution for inner types
      if (pyInner === tsInner) {
        // Inner types match, just check nullability
      } else if (resolveAlias(pyInner) === tsInner || pyInner === resolveAlias(tsInner) || resolveAlias(pyInner) === resolveAlias(tsInner)) {
        // Inner types match via alias — compatible (nullability check below)
      } else {
        return typesAreCompatible(pyInner, tsInner);
      }
    } else {
      // Non-array: check alias resolution
      if (resolveAlias(pyBase) !== tsBase && pyBase !== resolveAlias(tsBase) && resolveAlias(pyBase) !== resolveAlias(tsBase)) {
        return false;
      }
    }
  }

  // Nullability: TS should be nullable when Python is nullable.
  // (It's OK for TS to be nullable when Python is not — defensive typing.)
  // It's NOT OK for Python to be nullable and TS to be non-nullable.
  if (pyNull && !tsNull) return false;

  return true;
}

/**
 * Fields where the type check is intentionally skipped due to known
 * acceptable divergence (e.g., TS uses a branded union type where Python uses
 * plain str). Key format: "InterfaceName.fieldName".
 */
const TYPE_CHECK_SKIP: Set<string> = new Set([
  // Add entries here if intentional divergences arise, e.g.:
  // "SomeInterface.someField",
]);

// ---------------------------------------------------------------------------
// Backend → Frontend name aliases
// ---------------------------------------------------------------------------

/**
 * Explicit mapping for backend schema names that use different names on the
 * frontend. Key = backend class name, Value = frontend interface name.
 *
 * The test verifies that the frontend interface exists under the aliased name
 * and that a `type` alias from backend→frontend name is exported from index.ts.
 */
const BACKEND_TO_FRONTEND_ALIASES: Record<string, string> = {
  AuditLogItem: "AuditLogEntry",
  AuditLogListResponse: "AuditLogList",
};

// ---------------------------------------------------------------------------
// Test setup
// ---------------------------------------------------------------------------

const BACKEND_SCHEMAS_DIR = path.resolve(
  __dirname,
  "../../backend/app/schemas",
);
const FRONTEND_TYPES_PATH = path.resolve(__dirname, "../src/types/index.ts");

/**
 * Schema files to parse. Each entry maps a backend schema file to the Pydantic
 * model names we expect to find and verify against the frontend.
 * Using `"*"` means "all models found in the file".
 */
const SCHEMA_FILES = [
  "auth.py",
  "agent.py",
  "review.py",
  "workflow.py",
  "payer.py",
  "dashboard.py",
  "audit.py",
  "eligibility.py",
  "scheduling.py",
  "claims.py",
  "prior_auth.py",
  "credentialing.py",
  "compliance.py",
];

let frontendInterfaces: Map<string, Set<string>>;
let frontendFieldTypes: Map<string, Map<string, ParsedField>>; // interfaceName → field types
let backendModels: Map<string, string[]>; // modelName → field names
let backendFieldTypes: Map<string, Map<string, ParsedPydanticField>>; // modelName → field types

beforeAll(() => {
  // Parse frontend types
  const tsSource = fs.readFileSync(FRONTEND_TYPES_PATH, "utf-8");
  const parsed = parseInterfaces(tsSource);
  frontendInterfaces = new Map(
    parsed.map((iface) => [iface.name, new Set(iface.fields)]),
  );
  frontendFieldTypes = new Map(
    parsed.map((iface) => [iface.name, iface.fieldTypes]),
  );

  // Also detect `export type Alias = Target;` declarations for alias verification
  const aliasRe = /export\s+type\s+(\w+)\s*=\s*(\w+)\s*;/g;
  let aliasMatch: RegExpExecArray | null;
  while ((aliasMatch = aliasRe.exec(tsSource)) !== null) {
    const aliasName = aliasMatch[1];
    const targetName = aliasMatch[2];
    // If the target interface exists, register the alias with the same fields
    const targetFields = frontendInterfaces.get(targetName);
    if (targetFields) {
      frontendInterfaces.set(aliasName, targetFields);
    }
    const targetTypes = frontendFieldTypes.get(targetName);
    if (targetTypes) {
      frontendFieldTypes.set(aliasName, targetTypes);
    }
  }

  // Parse all backend schema files
  backendModels = new Map();
  backendFieldTypes = new Map();
  for (const file of SCHEMA_FILES) {
    const filePath = path.join(BACKEND_SCHEMAS_DIR, file);
    if (!fs.existsSync(filePath)) continue;
    const pySource = fs.readFileSync(filePath, "utf-8");
    const models = parsePydanticModels(pySource);
    for (const model of models) {
      backendModels.set(model.name, model.fields);
      backendFieldTypes.set(model.name, model.fieldTypes);
    }
  }
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("Schema parity: frontend TypeScript types match backend Pydantic schemas", () => {
  it("backend schema directory exists and contains expected files", () => {
    expect(fs.existsSync(BACKEND_SCHEMAS_DIR)).toBe(true);
    for (const file of SCHEMA_FILES) {
      expect(
        fs.existsSync(path.join(BACKEND_SCHEMAS_DIR, file)),
        `Backend schema file missing: ${file}`,
      ).toBe(true);
    }
  });

  it("at least 20 backend models were parsed (sanity check)", () => {
    expect(backendModels.size).toBeGreaterThanOrEqual(20);
  });

  // Dynamically generate one test per backend model
  // We wrap in a describe so vitest discovers them after beforeAll runs
  describe("per-model field parity", () => {
    // Use a getter pattern so tests are generated after beforeAll
    const getModels = () => Array.from(backendModels.entries());

    // Since vitest collects tests synchronously, we read the models eagerly here
    const backendDir = path.resolve(__dirname, "../../backend/app/schemas");
    const allModels: Array<[string, string[]]> = [];
    // Also parse eagerly for type info so dynamic tests can reference it
    const eagerBackendFieldTypes = new Map<string, Map<string, ParsedPydanticField>>();
    const eagerFrontendFieldTypes = new Map<string, Map<string, ParsedField>>();

    for (const file of SCHEMA_FILES) {
      const filePath = path.join(backendDir, file);
      if (!fs.existsSync(filePath)) continue;
      const pySource = fs.readFileSync(filePath, "utf-8");
      const models = parsePydanticModels(pySource);
      for (const model of models) {
        allModels.push([model.name, model.fields]);
        eagerBackendFieldTypes.set(model.name, model.fieldTypes);
      }
    }

    // Parse frontend types eagerly for type-level checks
    const tsTypesPath = path.resolve(__dirname, "../src/types/index.ts");
    if (fs.existsSync(tsTypesPath)) {
      const tsSource = fs.readFileSync(tsTypesPath, "utf-8");
      const tsIfaces = parseInterfaces(tsSource);
      for (const iface of tsIfaces) {
        eagerFrontendFieldTypes.set(iface.name, iface.fieldTypes);
      }
      // Also register aliases
      const aliasRe = /export\s+type\s+(\w+)\s*=\s*(\w+)\s*;/g;
      let am: RegExpExecArray | null;
      while ((am = aliasRe.exec(tsSource)) !== null) {
        const target = eagerFrontendFieldTypes.get(am[2]);
        if (target) eagerFrontendFieldTypes.set(am[1], target);
      }
    }

    for (const [backendName, expectedFields] of allModels) {
      // Resolve the frontend name (may be aliased)
      const frontendName =
        BACKEND_TO_FRONTEND_ALIASES[backendName] ?? backendName;

      it(`${backendName}${frontendName !== backendName ? ` (→ ${frontendName})` : ""} exists in frontend with correct fields`, () => {
        // 1. Interface must exist under the frontend name
        expect(
          frontendInterfaces.has(frontendName),
          `Frontend types missing interface '${frontendName}' (backend: '${backendName}')`,
        ).toBe(true);

        const frontendFields = frontendInterfaces.get(frontendName)!;
        const expectedSet = new Set(expectedFields);

        // 2. No missing fields (backend has it, frontend doesn't)
        const missingInFrontend = expectedFields.filter(
          (f) => !frontendFields.has(f),
        );
        expect(
          missingInFrontend,
          `Frontend '${frontendName}' is missing backend fields: ${missingInFrontend.join(", ")}`,
        ).toEqual([]);

        // 3. No extra fields (frontend has it, backend doesn't)
        const extraInFrontend = [...frontendFields].filter(
          (f) => !expectedSet.has(f),
        );
        expect(
          extraInFrontend,
          `Frontend '${frontendName}' has extra fields not in backend '${backendName}': ${extraInFrontend.join(", ")}`,
        ).toEqual([]);
      });

      it(`${backendName}${frontendName !== backendName ? ` (→ ${frontendName})` : ""} field types are compatible`, () => {
        // Skip if the interface doesn't exist (caught by the field parity test above)
        if (!frontendFieldTypes.has(frontendName)) return;

        const bFieldTypes = backendFieldTypes.get(backendName);
        const fFieldTypes = frontendFieldTypes.get(frontendName);
        if (!bFieldTypes || !fFieldTypes) return;

        const incompatible: string[] = [];

        for (const [fieldName, pyField] of bFieldTypes) {
          const skipKey = `${frontendName}.${fieldName}`;
          if (TYPE_CHECK_SKIP.has(skipKey)) continue;

          const tsField = fFieldTypes.get(fieldName);
          if (!tsField) continue; // Missing field caught by field parity test

          const pyNorm = normalizePythonType(pyField.type);
          const tsNorm = normalizeTsType(tsField.type);

          // Also account for TS optional marker (`?:`) making the field nullable
          const tsEffective = tsField.optional && !tsNorm.endsWith("|null")
            ? tsNorm + "|null"
            : tsNorm;

          if (!typesAreCompatible(pyNorm, tsEffective)) {
            incompatible.push(
              `  ${fieldName}: backend="${pyField.type}" (→ ${pyNorm}) ≠ frontend="${tsField.type}${tsField.optional ? "?" : ""}" (→ ${tsEffective})`,
            );
          }
        }

        expect(
          incompatible,
          `Type incompatibilities in ${frontendName}:\n${incompatible.join("\n")}`,
        ).toEqual([]);
      });

      // If this model has an alias, verify the alias type export exists
      if (BACKEND_TO_FRONTEND_ALIASES[backendName]) {
        it(`type alias '${backendName}' is exported and resolves to '${frontendName}'`, () => {
          // The alias should also be registered (from our beforeAll alias scan)
          expect(
            frontendInterfaces.has(backendName),
            `Frontend types missing type alias '${backendName}' → '${frontendName}'. ` +
              `Add: export type ${backendName} = ${frontendName};`,
          ).toBe(true);
        });
      }
    }
  });

  describe("type normalization sanity checks", () => {
    it("Python atomic types map to correct TS equivalents", () => {
      expect(normalizePythonType("str")).toBe("string");
      expect(normalizePythonType("uuid.UUID")).toBe("string");
      expect(normalizePythonType("datetime")).toBe("string");
      expect(normalizePythonType("int")).toBe("number");
      expect(normalizePythonType("float")).toBe("number");
      expect(normalizePythonType("bool")).toBe("boolean");
    });

    it("Python nullable types include |null", () => {
      expect(normalizePythonType("str | None")).toBe("string|null");
      expect(normalizePythonType("uuid.UUID | None")).toBe("string|null");
      expect(normalizePythonType("float | None")).toBe("number|null");
    });

    it("Python collection types map correctly", () => {
      expect(normalizePythonType("dict[str, Any]")).toBe("object");
      expect(normalizePythonType("list[str]")).toBe("array:string");
      expect(normalizePythonType("list[dict[str, Any]]")).toBe("array:object");
    });

    it("TS types normalize correctly", () => {
      expect(normalizeTsType("string")).toBe("string");
      expect(normalizeTsType("number")).toBe("number");
      expect(normalizeTsType("boolean")).toBe("boolean");
      expect(normalizeTsType("string | null")).toBe("string|null");
      expect(normalizeTsType("Record<string, unknown>")).toBe("object");
      expect(normalizeTsType("Record<string, unknown> | null")).toBe("object|null");
      expect(normalizeTsType("string[]")).toBe("array:string");
      expect(normalizeTsType("Array<Record<string, unknown>>")).toBe("array:object");
    });

    it("compatibility checker accepts valid pairs", () => {
      expect(typesAreCompatible("string", "string")).toBe(true);
      expect(typesAreCompatible("string|null", "string|null")).toBe(true);
      expect(typesAreCompatible("number", "number")).toBe(true);
      expect(typesAreCompatible("object", "object")).toBe(true);
      // TS can be nullable when Python is not (defensive typing)
      expect(typesAreCompatible("string", "string|null")).toBe(true);
    });

    it("compatibility checker rejects invalid pairs", () => {
      // Python nullable, TS non-nullable → bad
      expect(typesAreCompatible("string|null", "string")).toBe(false);
      // Type mismatch
      expect(typesAreCompatible("string", "number")).toBe(false);
      expect(typesAreCompatible("boolean", "string")).toBe(false);
    });
  });

  it("frontend does not define unexpected interfaces missing from backend", () => {
    const backendNames = new Set<string>();
    for (const name of backendModels.keys()) {
      backendNames.add(name);
      // Also add the frontend alias name
      const alias = BACKEND_TO_FRONTEND_ALIASES[name];
      if (alias) backendNames.add(alias);
    }

    // Known frontend-only types that don't correspond to backend Pydantic models
    const knownFrontendOnly = new Set([
      "WsTaskStatusChanged",
      "WsPong",
    ]);

    const frontendOnly = [...frontendInterfaces.keys()].filter(
      (n) => !backendNames.has(n) && !knownFrontendOnly.has(n),
    );

    if (frontendOnly.length > 0) {
      console.warn(
        `[schema-parity] Frontend-only interfaces (not in backend): ${frontendOnly.join(", ")}`,
      );
    }
    // Don't fail — just warn. Frontend may legitimately have extra UI-only types.
  });
});
