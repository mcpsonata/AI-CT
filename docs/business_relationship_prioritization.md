# Business Relationship Prioritization System

## Overview

The `evaluate_table_linking` function in `src/server.py` implements a sophisticated business-focused prioritization system for Power BI table relationships. This system categorizes and prioritizes relationships based on their business criticality and impact on analytics capabilities.

## Table Categorization System

### 1. Table Type Classification

The system automatically categorizes tables into business-relevant types:

```python
# Table categorization logic (Line 5311-5317)
fact_tables = [t for t in tables if t.lower().startswith('fact')]
dim_tables = [t for t in tables if t.lower().startswith('dim')]
date_tables = [t for t in dim_tables if 'date' in t.lower() or 'time' in t.lower()]
security_tables = [t for t in tables if 'sec_' in t.lower() or 'security' in t.lower()]
```

### 2. Table Type Priorities

The system assigns priority scores based on business criticality:

| Table Type | Priority Score | Business Impact |
|------------|----------------|-----------------|
| Security | 0.35 | Highest - Data governance & compliance |
| Fact | 0.30 | High - Core business metrics |
| Dimension | 0.25 | Medium-High - Reference data |
| Bridge | 0.20 | Medium - Supporting infrastructure |
| Other | 0.15 | Base - General tables |

## Critical Relationship Types

### 1. Critical Date Relationships

**Purpose**: Essential for time intelligence and fiscal period analysis

**Priority**: Highest (Confidence Score: 0.95)

**Implementation**:
```python
# 1. CRITICAL DATE RELATIONSHIPS - Only if actual matching columns exist
logger.info("Analyzing date relationships with actual column verification...")
for date_table in date_tables:
    date_columns = get_actual_columns(date_table)
    date_join_columns = [col for col in date_columns if 'fiscalmonthid' in col.lower() or 'dateid' in col.lower()]
   
    for fact_table in fact_tables:
        if (fact_table, date_table) not in existing_pairs:
            fact_columns = get_actual_columns(fact_table)
           
            # Look for matching fiscal month ID columns
            fact_fiscal_columns = [col for col in fact_columns if 'fiscalmonthid' in col.lower()]
            date_fiscal_columns = [col for col in date_columns if 'fiscalmonthid' in col.lower()]
           
            if fact_fiscal_columns and date_fiscal_columns:
                potential_relationships.append({
                    "from_table": fact_table,
                    "from_column": fact_fiscal_columns[0],
                    "to_table": date_table,
                    "to_column": date_fiscal_columns[0],
                    "suggested_cardinality": "ManyToOne",
                    "confidence_score": 0.95,
                    "reason": f"Critical date relationship: {fact_table}.{fact_fiscal_columns[0]} → {date_table}.{date_fiscal_columns[0]}",
                    "business_justification": "Essential for time intelligence calculations and fiscal period analysis"
                })
```

**Key Features**:
- Verifies actual column existence using DAX queries
- Looks for fiscal month ID and date ID patterns
- Suggests Many-to-One cardinality (fact to date dimension)
- Provides clear business justification

### 2. Critical Security Relationships

**Purpose**: Essential for row-level security (RLS) implementation

**Priority**: Second Highest (Confidence Score: 0.90)

**Implementation**:
```python
# 2. CRITICAL SECURITY RELATIONSHIPS - Only if actual matching columns exist
logger.info("Analyzing security relationships with actual column verification...")
for sec_table in security_tables:
    sec_columns = get_actual_columns(sec_table)
   
    # Look for business hierarchy key columns in security table
    sec_business_keys = [col for col in sec_columns if 'dimbusinesshierarchykey' in col.lower()]
   
    if sec_business_keys:
        # Find business hierarchy dimension
        business_dims = [t for t in dim_tables if 'business' in t.lower() and 'hierarchy' in t.lower()]
       
        for business_dim in business_dims:
            if (sec_table, business_dim) not in existing_pairs:
                business_columns = get_actual_columns(business_dim)
                business_keys = [col for col in business_columns if 'dimbusinesshierarchykey' in col.lower()]
               
                if business_keys:
                    potential_relationships.append({
                        "from_table": sec_table,
                        "from_column": sec_business_keys[0],
                        "to_table": business_dim,
                        "to_column": business_keys[0],
                        "suggested_cardinality": "ManyToOne",
                        "confidence_score": 0.90,
                        "reason": f"Critical security relationship: {sec_table}.{sec_business_keys[0]} → {business_dim}.{business_keys[0]}",
                        "business_justification": "Essential for row-level security implementation and user-based data access controls"
                    })
```

**Key Features**:
- Identifies security tables using naming patterns (`sec_`, `security`)
- Looks for business hierarchy key connections
- Critical for data governance and user access control
- Enables row-level security functionality

### 3. Missing Fact-to-Dimension Relationships

**Purpose**: Core analytical relationships for business reporting

**Priority**: High (Confidence Score: 0.85)

**Implementation**:
```python
# 3. MISSING FACT-TO-DIMENSION RELATIONSHIPS - Only verify actual columns
logger.info("Analyzing missing fact-to-dimension relationships...")
for fact_table in fact_tables:
    fact_columns = get_actual_columns(fact_table)
    fact_connections = set()
   
    for rel in existing_rels:
        if rel["from_table"] == fact_table:
            fact_connections.add(rel["to_table"])
   
    # Check for missing product hierarchy connections
    product_dims = [t for t in dim_tables if 'product' in t.lower() and 'hierarchy' in t.lower()]
    for product_dim in product_dims:
        if product_dim not in fact_connections:
            product_columns = get_actual_columns(product_dim)
           
            fact_product_keys = [col for col in fact_columns if 'dimproducthierarchykey' in col.lower()]
            dim_product_keys = [col for col in product_columns if 'dimproducthierarchykey' in col.lower()]
           
            if fact_product_keys and dim_product_keys:
                potential_relationships.append({
                    "from_table": fact_table,
                    "from_column": fact_product_keys[0],
                    "to_table": product_dim,
                    "to_column": dim_product_keys[0],
                    "suggested_cardinality": "ManyToOne",
                    "confidence_score": 0.85,
                    "reason": f"Missing product dimension: {fact_table}.{fact_product_keys[0]} → {product_dim}.{dim_product_keys[0]}",
                    "business_justification": "Essential for product-based analysis and reporting"
                })
```

**Key Features**:
- Identifies missing connections between fact and dimension tables
- Focuses on product hierarchy relationships
- Ensures core analytical capabilities are available
- Prevents isolated fact tables

## Priority Scoring Algorithm

### Table Type Detection

The system uses a sophisticated table type detection algorithm:

```python
def _detect_table_type_priority(table_name: str, column_name: str = "") -> tuple:
    """
    Detect table type and return (priority_score, table_type)
    Higher priority score = more business critical
    """
    table_lower = table_name.lower()
    column_lower = column_name.lower() if column_name else ""
    
    # Security tables/columns - Highest Priority (0.35)
    security_indicators = [
        'security', 'sec_', 'auth', 'permission', 'role', 'access', 
        'user_role', 'security_role', 'rls_', 'row_level', 'policy'
    ]
    if any(indicator in table_lower for indicator in security_indicators) or \
       any(indicator in column_lower for indicator in security_indicators):
        return (0.35, "Security")
    
    # Bridge tables/columns - Check BEFORE dimensions (more specific matching)
    bridge_indicators = [
        'bridge', 'junction', 'link', 'mapping', 'cross_reference', 'crossreference',
        'xref', 'association', 'relationship', 'many_to_many'
    ]
    if any(indicator in table_lower for indicator in bridge_indicators) or \
       any(indicator in column_lower for indicator in bridge_indicators):
        return (0.20, "Bridge")
    
    # Fact tables/columns - High Priority (0.30)
    fact_indicators = [
        'fact', 'sales', 'revenue', 'transaction', 'order', 'invoice', 
        'payment', 'billing', 'metric', 'measure', 'kpi', 'performance'
    ]
    if any(indicator in table_lower for indicator in fact_indicators) or \
       any(indicator in column_lower for indicator in fact_indicators):
        return (0.30, "Fact")
    
    # Dimension tables/columns - Medium-High Priority (0.25)
    dimension_indicators = [
        'dim', 'dimension', 'lookup', 'reference', 'master', 'customer', 
        'product', 'employee', 'location', 'date', 'time', 'calendar',
        'geography', 'category', 'type', 'status'
    ]
    if (any(indicator in table_lower for indicator in dimension_indicators) or 
        any(indicator in column_lower for indicator in dimension_indicators)):
        return (0.25, "Dimension")
    
    # Default tables - Base Priority (0.15)
    return (0.15, "Other")
```

### Pattern Recognition

#### Security Table Indicators
- `security`, `sec_`, `auth`, `permission`, `role`, `access`
- `user_role`, `security_role`, `rls_`, `row_level`, `policy`

#### Fact Table Indicators
- `fact`, `sales`, `revenue`, `transaction`, `order`, `invoice`
- `payment`, `billing`, `metric`, `measure`, `kpi`, `performance`

#### Dimension Table Indicators
- `dim`, `dimension`, `lookup`, `reference`, `master`
- `customer`, `product`, `employee`, `location`, `date`, `time`, `calendar`
- `geography`, `category`, `type`, `status`

#### Bridge Table Indicators
- `bridge`, `junction`, `link`, `mapping`, `cross_reference`
- `xref`, `association`, `relationship`, `many_to_many`

## Smart Features

### 1. Actual Column Verification

The system verifies that suggested relationships use columns that actually exist:

```python
def get_actual_columns(table_name):
    """Get actual column names from table using DAX query"""
    try:
        dax_query = f"EVALUATE TOPN(1, {table_name})"
        result = self.tabular_editor.execute_dax_query(dax_query)
        if result and len(result) > 0:
            return list(result[0].keys())
        return []
    except:
        return []
```

### 2. Existing Relationship Check

Prevents duplicate relationship suggestions:

```python
existing_pairs = set()
for rel in existing_rels:
    existing_pairs.add((rel["from_table"], rel["to_table"]))
    existing_pairs.add((rel["to_table"], rel["from_table"]))

# Check if relationship already exists
if (fact_table, date_table) not in existing_pairs:
    # Suggest new relationship
```

### 3. Business Justification

Each suggested relationship includes clear business reasoning:

- **Date Relationships**: "Essential for time intelligence calculations and fiscal period analysis"
- **Security Relationships**: "Essential for row-level security implementation and user-based data access controls"
- **Fact-Dimension Relationships**: "Essential for product-based analysis and reporting"

## Recommendation Categories

The system generates prioritized recommendations:

### Critical Priority
- Isolated fact tables (prevents all analysis)
- Missing critical relationships with verified columns

### High Priority
- Isolated security tables (prevents RLS functionality)

### Medium Priority
- Isolated dimension tables (limits filtering capabilities)

## Usage Guidelines

### When to Use This System

1. **Initial Model Assessment**: Evaluate new Power BI models for missing critical relationships
2. **Model Optimization**: Identify high-priority relationships to implement first
3. **Data Governance**: Ensure security relationships are properly established
4. **Performance Optimization**: Focus on relationships that enable core analytics

### Best Practices

1. **Implement by Priority**: Start with Critical (0.95+ confidence), then High (0.90+), then Medium (0.85+)
2. **Verify Column Existence**: System already does this, but double-check before implementation
3. **Test Relationships**: Validate suggested cardinality matches actual data patterns
4. **Monitor Performance**: Some relationships may impact query performance

### Limitations

1. **Naming Convention Dependent**: Relies on specific table/column naming patterns
2. **Business Context Specific**: Designed for specific business intelligence scenarios
3. **Pattern-Based**: May miss relationships with non-standard naming

## Extension Points

### Adding New Relationship Types

To add new critical relationship types:

1. **Define Table Category**: Add new table classification logic
2. **Set Priority Score**: Assign appropriate business priority (0.0-0.35)
3. **Add Detection Logic**: Create pattern matching for table/column names
4. **Include Business Justification**: Explain why this relationship is critical

### Customizing Priority Scores

Priority scores can be adjusted based on organizational needs:

```python
# Adjust these values based on business priorities
SECURITY_PRIORITY = 0.35  # Highest for compliance-focused orgs
FACT_PRIORITY = 0.30      # High for analytics-focused orgs
DIMENSION_PRIORITY = 0.25 # Medium-high for reporting needs
BRIDGE_PRIORITY = 0.20    # Medium for complex data models
DEFAULT_PRIORITY = 0.15   # Base level
```

## Performance Considerations

### Optimization Strategies

1. **Parallel Processing**: Process different relationship types concurrently
2. **Caching**: Cache column information to avoid repeated DAX queries
3. **Batching**: Group similar operations to reduce round trips
4. **Early Exit**: Stop processing when high-confidence relationships are found

### Monitoring

- Track DAX query execution time
- Monitor memory usage during analysis
- Log relationship suggestion accuracy
- Measure business impact of implemented relationships

## Conclusion

This prioritization system ensures that the most business-critical relationships are identified and implemented first, maximizing the value and usability of Power BI data models while maintaining data governance and security requirements.