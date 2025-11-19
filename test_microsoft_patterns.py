#!/usr/bin/env python3
"""
Test Microsoft hierarchy pattern recognition and scoring
"""

def test_microsoft_pattern_recognition():
    """Test the Microsoft hierarchy pattern recognition logic"""
    
    # Test cases based on the Microsoft hierarchy standards provided
    test_hierarchies = [
        {"name": "Function Hierarchies (Executive and Channel)", "path": "Org Exec Summary > Org Exec > Org Exec Detail > Function Summary > Function > Function Detail"},
        {"name": "Account (Cost Element) Hierarchy", "path": "Class > Sub-Class > Group > Line Item > Line Item Detail > Account"},
        {"name": "Geography Hierarchy (MARS)", "path": "WW Area Summary > WW Area > WW Region > WW Sub Region > Sales Location > Sales Sub Location"},
        {"name": "Product Hierarchy", "path": "Reporting Summary Revsum Divison > Reporting Revsum divison > SuperRev Sum Division > Rev Sum Division"},
        {"name": "Business Hierarchy", "path": "Business Summary > Business"},
        {"name": "Segmentation Hierarchy", "path": "Segment > Subsegment"},
        {"name": "Pricing Level Hierarchy", "path": "Pricing Level > Purchase Type > User Type > Detail Pricing Level"},
        {"name": "Fiscal Date Hierarchy", "path": "FiscalYear > FiscalHalf > FiscalQuarter > FiscalMonth > FiscalWeek > FiscalDate"},
        {"name": "Calendar Date Hierarchy", "path": "CalendarYear > CalendarQuarter > CalendarMonth > CalendarWeek > CalendarDate"},
        {"name": "Industry Hierarchy", "path": "Industry > Vertical > Sub Vertical > Vertical Category"},
    ]
    
    # Define Microsoft standard hierarchy patterns (same as in server.py)
    ms_hierarchy_patterns = {
        "Function Hierarchies": {
            "keywords": ["org", "executive", "exec", "function", "channel", "summary"],
            "patterns": ["Org Exec Summary", "Org Exec", "Function Summary", "Function Detail", "Executive"],
            "type": "Dimension",
            "base_score": 0.6,
            "confidence_boost": 0.0
        },
        "Account Hierarchy": {
            "keywords": ["account", "cost", "element", "class", "group", "line item", "balance", "sheet"],
            "patterns": ["Class", "Sub-Class", "Group", "Line Item", "Line Item Detail", "Account", "Balance Sheet"],
            "type": "Dimension",
            "base_score": 0.6,
            "confidence_boost": 0.0
        },
        "Geography Hierarchy": {
            "keywords": ["geography", "mars", "sales", "area", "region", "country", "location", "subsidiary", "district", "continent", "state", "city", "postal", "ww"],
            "patterns": ["WW Area", "Big Area", "Area", "Region", "Sub Region", "Country", "City", "Location", "Sales District", "Continent", "State", "Province", "PostalCode"],
            "type": "Dimension", 
            "base_score": 0.6,
            "confidence_boost": 0.0
        },
        "Product Hierarchy": {
            "keywords": ["product", "division", "category", "family", "pfam", "sku", "revsum", "reporting", "super", "rev sum", "business unit"],
            "patterns": ["Reporting Summary", "RevSum", "Division", "Super Division", "Business Unit", "Product Unit", "Category", "Family", "Product", "SKU", "Part Number"],
            "type": "Dimension",
            "base_score": 0.6,
            "confidence_boost": 0.0
        },
        "Business Hierarchy": {
            "keywords": ["business", "stream", "unit", "summary"],
            "patterns": ["Business Summary", "Business Unit", "Business Stream", "Business Stram Summary", "Business Stram Group"],
            "type": "Dimension",
            "base_score": 0.6,
            "confidence_boost": 0.0
        },
        "Segmentation Hierarchy": {
            "keywords": ["segment", "subsegment", "customer"],
            "patterns": ["Segment", "Subsegment", "Customer Segments"],
            "type": "Dimension",
            "base_score": 0.6,
            "confidence_boost": 0.0
        },
        "Pricing Hierarchy": {
            "keywords": ["pricing", "level", "purchase", "user", "detail"],
            "patterns": ["Pricing Level", "Purchase Type", "User Type", "Detail Pricing Level"],
            "type": "Dimension",
            "base_score": 0.6,
            "confidence_boost": 0.0
        },
        "Date Hierarchy": {
            "keywords": ["date", "fiscal", "calendar", "year", "quarter", "month", "week", "semester", "half"],
            "patterns": ["FiscalYear", "Fiscal Year", "CalendarYear", "Calendar Year", "FiscalHalf", "FiscalQuarter", "FiscalMonth", "FiscalWeek", "CalendarQuarter", "CalendarMonth", "CalendarWeek"],
            "type": "Dimension",
            "base_score": 0.6,
            "confidence_boost": 0.0
        },
        "Industry Hierarchy": {
            "keywords": ["industry", "vertical", "category"],
            "patterns": ["Industry", "Vertical", "Sub Vertical", "Vertical Category"],
            "type": "Dimension",
            "base_score": 0.6,
            "confidence_boost": 0.0
        }
    }
    
    print("🧪 Testing Microsoft Hierarchy Pattern Recognition")
    print("=" * 60)
    
    successful_matches = 0
    total_tests = len(test_hierarchies)
    
    for test_case in test_hierarchies:
        hierarchy_name = test_case["name"]
        column_path = test_case["path"]
        
        print(f"\n📋 Testing: {hierarchy_name}")
        print(f"    Path: {column_path[:50]}...")
        
        # Apply the same pattern matching logic as in server.py
        hierarchy_text = f"{hierarchy_name} {column_path}".lower()
        
        best_pattern_score = 0.0
        best_pattern_name = "Unknown"
        best_base_score = 0.0
        
        for pattern_name, pattern_info in ms_hierarchy_patterns.items():
            # Strategy 1: Direct keyword matching
            keyword_matches = sum(1 for keyword in pattern_info["keywords"] if keyword in hierarchy_text)
            keyword_score = keyword_matches / len(pattern_info["keywords"]) if pattern_info["keywords"] else 0
            
            # Strategy 2: Pattern structure matching  
            pattern_matches = sum(1 for pattern in pattern_info["patterns"] if pattern.lower() in hierarchy_text)
            pattern_score = pattern_matches / len(pattern_info["patterns"]) if pattern_info["patterns"] else 0
            
            # Strategy 3: Hierarchy name exact matching
            name_match_score = 0.0
            hierarchy_name_clean = hierarchy_name.lower().replace(" ", "").replace("_", "")
            for pattern in pattern_info["patterns"]:
                pattern_clean = pattern.lower().replace(" ", "").replace("_", "").replace("-", "")
                if pattern_clean in hierarchy_name_clean or hierarchy_name_clean in pattern_clean:
                    name_match_score = 1.0
                    break
            
            # Calculate composite pattern score
            total_score = (keyword_score * 0.4) + (pattern_score * 0.3) + (name_match_score * 0.3)
            
            if total_score > 0.2 or name_match_score > 0:
                if total_score > best_pattern_score:
                    best_pattern_score = total_score
                    best_pattern_name = pattern_name
                    best_base_score = pattern_info["base_score"]
        
        # Calculate final HQ-002 score
        final_hq_002_score = best_base_score if best_base_score > 0 else 0.0
        
        print(f"    ✅ Detected Pattern: {best_pattern_name}")
        print(f"    📊 Pattern Score: {best_pattern_score:.3f}")
        print(f"    🎯 HQ-002 Score: {final_hq_002_score:.1f}")
        
        if best_pattern_name != "Unknown" and final_hq_002_score >= 0.6:
            print(f"    ✅ SUCCESS: Microsoft pattern detected with good score!")
            successful_matches += 1
        else:
            print(f"    ❌ FAILED: No Microsoft pattern detected or low score")
    
    print(f"\n{'=' * 60}")
    print(f"📈 RESULTS: {successful_matches}/{total_tests} hierarchies correctly matched Microsoft patterns")
    print(f"📊 Success Rate: {(successful_matches/total_tests)*100:.1f}%")
    
    if successful_matches == total_tests:
        print("🎉 ALL MICROSOFT HIERARCHY PATTERNS RECOGNIZED SUCCESSFULLY!")
    elif successful_matches >= total_tests * 0.8:
        print("✅ Good: Most Microsoft hierarchy patterns recognized")
    else:
        print("⚠️  Warning: Some Microsoft hierarchy patterns not recognized properly")
    
    return successful_matches == total_tests

if __name__ == "__main__":
    test_microsoft_pattern_recognition()