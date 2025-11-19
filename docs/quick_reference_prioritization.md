# Quick Reference: Business Relationship Prioritization

## Priority Scores (Highest to Lowest)

| Type | Score | Pattern Examples | Business Impact |
|------|-------|------------------|-----------------|
| **Security** | 0.35 | `sec_`, `security`, `auth`, `rls_` | Data governance, compliance |
| **Fact** | 0.30 | `fact`, `sales`, `revenue`, `transaction` | Core business metrics |
| **Dimension** | 0.25 | `dim`, `customer`, `product`, `date` | Reference data, filtering |
| **Bridge** | 0.20 | `bridge`, `junction`, `mapping` | Complex relationships |
| **Other** | 0.15 | Any other table | General purpose |

## Confidence Scores

| Relationship Type | Score | Why |
|-------------------|-------|-----|
| **Date → Fact** | 0.95 | Essential for time intelligence |
| **Security → Business** | 0.90 | Required for row-level security |
| **Fact → Dimension** | 0.85 | Core analytical relationships |

## Column Pattern Matching

### Date Relationships
```python
# Looks for these patterns in column names
date_patterns = ['fiscalmonthid', 'dateid']
```

### Security Relationships  
```python
# Looks for these patterns in column names
security_patterns = ['dimbusinesshierarchykey']
```

### Fact-Dimension Relationships
```python
# Looks for these patterns in column names
product_patterns = ['dimproducthierarchykey']
```

## Table Categorization Logic

```python
# Fact tables
fact_tables = [t for t in tables if t.lower().startswith('fact')]

# Dimension tables
dim_tables = [t for t in tables if t.lower().startswith('dim')]

# Date tables (subset of dimensions)
date_tables = [t for t in dim_tables if 'date' in t.lower() or 'time' in t.lower()]

# Security tables
security_tables = [t for t in tables if 'sec_' in t.lower() or 'security' in t.lower()]
```

## Key Functions

### Column Verification
```python
def get_actual_columns(table_name):
    """Returns actual column names from table using DAX"""
    dax_query = f"EVALUATE TOPN(1, {table_name})"
    result = self.tabular_editor.execute_dax_query(dax_query)
    return list(result[0].keys()) if result else []
```

### Priority Detection
```python
def _detect_table_type_priority(table_name: str, column_name: str = "") -> tuple:
    """Returns (priority_score, table_type)"""
    # Returns values like (0.35, "Security") or (0.30, "Fact")
```

## Business Justifications

| Relationship | Justification |
|--------------|---------------|
| **Date** | "Essential for time intelligence calculations and fiscal period analysis" |
| **Security** | "Essential for row-level security implementation and user-based data access controls" |
| **Product** | "Essential for product-based analysis and reporting" |

## Critical Analysis Sections

### 1. Date Relationships (Lines 5330-5353)
- **Purpose**: Time intelligence
- **Pattern**: Fact tables → Date dimensions
- **Columns**: `fiscalmonthid`, `dateid`
- **Cardinality**: ManyToOne

### 2. Security Relationships (Lines 5355-5383)
- **Purpose**: Row-level security
- **Pattern**: Security tables → Business hierarchy
- **Columns**: `dimbusinesshierarchykey`
- **Cardinality**: ManyToOne

### 3. Fact-Dimension Relationships (Lines 5385-5407)
- **Purpose**: Core analytics
- **Pattern**: Fact tables → Product dimensions
- **Columns**: `dimproducthierarchykey`
- **Cardinality**: ManyToOne

## Performance Impact

| Operation | Cost | Count |
|-----------|------|-------|
| **Table listing** | Low | 1 query |
| **Column enumeration** | Medium | 1 per table |
| **Data validation** | High | 1 per relationship |

## Quick Diagnostics

### Check if function will find relationships:
1. **Table names** start with `fact`, `dim`, contain `sec_`
2. **Column names** contain expected patterns
3. **No existing relationships** between the tables
4. **DAX queries succeed** (connection working)

### Common Issues:
- **No suggestions**: Check table naming conventions
- **Low confidence**: Verify column name patterns
- **Performance slow**: Too many DAX queries
- **Errors**: Check database connection

## Extension Pattern

To add new relationship type:

```python
# 1. Add table categorization
special_tables = [t for t in tables if 'special_' in t.lower()]

# 2. Add analysis loop
for special_table in special_tables:
    special_columns = get_actual_columns(special_table)
    # Pattern matching logic...
    
    if matching_columns_found:
        potential_relationships.append({
            "from_table": from_table,
            "from_column": from_column,
            "to_table": to_table, 
            "to_column": to_column,
            "suggested_cardinality": "ManyToOne",
            "confidence_score": 0.80,  # Set appropriate confidence
            "reason": f"Special relationship: {description}",
            "business_justification": "Business reason here"
        })

# 3. Add priority detection
if 'special' in table_lower:
    return (0.28, "Special")  # Set appropriate priority
```

## File Locations

- **Main function**: `src/server.py` → `evaluate_table_linking()`
- **Documentation**: `docs/business_relationship_prioritization.md`
- **Technical details**: `docs/technical_implementation_table_linking.md`