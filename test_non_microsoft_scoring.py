#!/usr/bin/env python3
"""
Test how non-Microsoft hierarchy patterns are scored
"""

def demonstrate_non_microsoft_pattern_scoring():
    """Show how hierarchies that don't match Microsoft patterns get scored"""
    
    print("🧪 Demonstrating Non-Microsoft Pattern Scoring")
    print("=" * 60)
    
    # Example non-Microsoft hierarchies (custom/generic patterns)
    test_hierarchies = [
        {"name": "Custom Sales Hierarchy", "path": "Territory > Zone > Manager > Representative"},
        {"name": "Random Data Structure", "path": "Level1 > Level2 > Level3"},
        {"name": "Weird Naming Convention", "path": "TopNode > MiddleNode > LeafNode"},
        {"name": "Non-Standard Product", "path": "SuperCategory > Category > Item > Variant > Detail"},
        {"name": "Custom Organization", "path": "Mega-Division > Sub-Division > Department > Team > Person"},
    ]
    
    # Microsoft patterns for comparison
    ms_hierarchy_patterns = {
        "Function Hierarchies": {
            "keywords": ["org", "executive", "exec", "function", "channel", "summary"],
            "patterns": ["Org Exec Summary", "Org Exec", "Function Summary", "Function Detail", "Executive"],
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
        }
    }
    
    print("\n📊 How Non-Microsoft Patterns Are Scored:")
    print("-" * 40)
    print("1. **Microsoft Pattern Detection**: First, the system tries to match against Microsoft patterns")
    print("2. **Pattern Matching Logic**: Uses keywords, structure patterns, and name matching")
    print("3. **Fallback to AI Scoring**: If no Microsoft pattern detected, uses AI evaluation")
    print("4. **AI Scoring Criteria**:")
    print("   - 0.6 points: Perfect Microsoft pattern + optimal levels + correct direction")
    print("   - 0.45 points: Microsoft pattern + either optimal levels OR correct direction") 
    print("   - 0.3 points: Basic pattern recognition but suboptimal structure")
    print("   - 0.0-0.15 points: No clear pattern or problematic structure")
    
    print(f"\n🔍 Testing Non-Microsoft Hierarchies:")
    
    for test_case in test_hierarchies:
        hierarchy_name = test_case["name"]
        column_path = test_case["path"]
        
        print(f"\n📋 Testing: {hierarchy_name}")
        print(f"    Path: {column_path}")
        
        # Apply Microsoft pattern detection
        hierarchy_text = f"{hierarchy_name} {column_path}".lower()
        
        microsoft_pattern_detected = False
        best_pattern_score = 0.0
        
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
                    microsoft_pattern_detected = True
        
        if microsoft_pattern_detected:
            print(f"    ✅ Microsoft Pattern Detected: Gets base score 0.6")
        else:
            print(f"    ❌ No Microsoft Pattern: Falls back to AI evaluation")
            print(f"    🤖 AI Scoring Logic:")
            
            # Simulate AI scoring logic based on the prompt criteria
            level_count = len(column_path.split(" > "))
            
            # Basic pattern analysis
            if any(keyword in hierarchy_text for keyword in ["sales", "territory", "manager"]):
                simulated_ai_score = 0.3  # Basic pattern recognition
                print(f"        📊 Detects sales/organizational pattern: 0.3 points")
            elif any(keyword in hierarchy_text for keyword in ["category", "item", "product"]):
                simulated_ai_score = 0.35  # Partial product pattern
                print(f"        📊 Detects partial product pattern: 0.35 points") 
            elif any(keyword in hierarchy_text for keyword in ["division", "department", "team"]):
                simulated_ai_score = 0.25  # Basic organizational structure
                print(f"        📊 Detects organizational structure: 0.25 points")
            elif level_count >= 3 and level_count <= 6:
                simulated_ai_score = 0.2   # Reasonable level count
                print(f"        📊 Reasonable level count ({level_count}): 0.2 points")
            else:
                simulated_ai_score = 0.1   # Generic/unclear pattern
                print(f"        📊 Generic/unclear pattern: 0.1 points")
            
            # Level optimization penalty/bonus
            if level_count > 7:
                simulated_ai_score -= 0.05
                print(f"        ⚠️  Too many levels ({level_count}): -0.05 points")
            elif level_count < 2:
                simulated_ai_score -= 0.05  
                print(f"        ⚠️  Too few levels ({level_count}): -0.05 points")
            
            simulated_ai_score = max(0.0, min(0.6, simulated_ai_score))  # Clamp to valid range
            
            print(f"    🎯 Final AI HQ-002 Score: {simulated_ai_score:.2f}")
            
            # Calculate total with dimension table assumption (0.4 + AI score)
            total_score = 0.4 + simulated_ai_score
            if total_score >= 0.8:
                category = "Excellent"
            elif total_score >= 0.6:
                category = "Good"
            elif total_score >= 0.4:
                category = "Fair" 
            else:
                category = "Poor"
                
            print(f"    📊 Total Score: {total_score:.2f}/1.0 ({category})")
    
    print(f"\n{'=' * 60}")
    print("📈 **Summary of Non-Microsoft Pattern Scoring:**")
    print()
    print("🔍 **Detection Process:**")
    print("   1. Try to match against Microsoft standard patterns")
    print("   2. If no match found, use AI evaluation with these criteria:")
    print()
    print("🎯 **AI Evaluation Criteria for Non-Microsoft Patterns:**")
    print("   • **Pattern Recognition** (0.0-0.4 points):")
    print("     - Clear business logic and semantic meaning")
    print("     - Recognizable organizational/sales/product structures")
    print("     - Logical level progression and naming")
    print()
    print("   • **Level Optimization** (0.0-0.2 points):")
    print("     - Optimal level count (3-6 levels typically)")
    print("     - Not too many levels (>7) or too few (<2)")
    print("     - Appropriate granularity for business analysis")
    print()
    print("   • **Direction & Structure** (0.0-0.1 points):")
    print("     - Clear hierarchy direction (High-to-Low preferred)")
    print("     - Consistent naming conventions")
    print("     - Logical business flow")
    print()
    print("⚖️ **Scoring Comparison:**")
    print("   • Microsoft Standard Patterns: **0.6/0.6** (guaranteed)")
    print("   • Good Non-Microsoft Patterns: **0.3-0.45/0.6** (AI evaluated)")
    print("   • Poor Non-Microsoft Patterns: **0.0-0.2/0.6** (AI evaluated)")
    print()
    print("📊 **Total Possible Scores:**")
    print("   • Microsoft + Dimension table: **1.0/1.0** (Excellent)")
    print("   • Good Custom + Dimension table: **0.7-0.85/1.0** (Good-Excellent)")
    print("   • Poor Custom + Dimension table: **0.4-0.6/1.0** (Fair-Good)")

if __name__ == "__main__":
    demonstrate_non_microsoft_pattern_scoring()