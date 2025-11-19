#!/usr/bin/env python3
"""
Test level-specific recommendations for non-Microsoft hierarchy patterns
"""

def test_level_specific_analysis():
    """Test the level-specific analysis logic for non-Microsoft patterns"""
    
    print("🧪 Testing Level-Specific Analysis for Non-Microsoft Patterns")
    print("=" * 70)
    
    # Test cases with various problematic level structures
    test_hierarchies = [
        {
            "name": "Poor Generic Hierarchy", 
            "path": "Level1 > Level2 > Level3 > Level4",
            "expected_issues": ["Generic naming"]
        },
        {
            "name": "Technical Naming", 
            "path": "DivisionID > DepartmentKey > TeamRef > PersonCode",
            "expected_issues": ["Technical naming"]
        },
        {
            "name": "Unclear Abbreviations", 
            "path": "DIV > DEP > TM > PER",
            "expected_issues": ["Unclear abbreviations"]
        },
        {
            "name": "Too Many Levels", 
            "path": "Corp > Div > SubDiv > Dept > SubDept > Team > SubTeam > Person > Role",
            "expected_issues": ["Too many levels"]
        },
        {
            "name": "Too Few Levels", 
            "path": "Company > Person",
            "expected_issues": ["Too few levels"]
        },
        {
            "name": "Good Custom Hierarchy", 
            "path": "Region > Territory > District > Store",
            "expected_issues": []
        },
        {
            "name": "Mixed Issues", 
            "path": "Level1 > department_name > TM > PersonID > random_field",
            "expected_issues": ["Generic naming", "Inconsistent convention", "Technical naming"]
        }
    ]
    
    print("🔍 **Level Analysis Logic:**")
    print("-" * 30)
    print("1. **Generic Naming Detection**: level, node, item, field, data, value")
    print("2. **Technical Naming Detection**: id, key, code, ref, num")  
    print("3. **Unclear Abbreviation Detection**: 3 or fewer uppercase chars")
    print("4. **Level Count Analysis**: 3-6 optimal, <3 too few, >7 too many")
    print("5. **Business Progression Check**: logical parent-child relationships")
    print("6. **Naming Consistency Check**: consistent style across levels")
    print()
    
    for i, test_case in enumerate(test_hierarchies, 1):
        hierarchy_name = test_case["name"]
        column_path = test_case["path"] 
        expected_issues = test_case["expected_issues"]
        
        print(f"📋 **Test {i}: {hierarchy_name}**")
        print(f"   Path: {column_path}")
        print(f"   Expected Issues: {', '.join(expected_issues) if expected_issues else 'None'}")
        
        # Apply the level-specific analysis logic
        level_specific_recommendations = []
        
        # Parse column path into individual levels
        if column_path and column_path != "Column details unavailable":
            levels = [level.strip() for level in column_path.split(" > ")]
            print(f"   Levels Found: {levels} ({len(levels)} levels)")
            
            # Analyze each level for naming and business logic issues
            for j, level in enumerate(levels):
                level_number = j + 1
                level_clean = level.lower().replace(" ", "").replace("_", "").replace("-", "")
                
                # Check for generic/poor naming patterns
                if any(generic in level_clean for generic in ["level", "node", "item", "field", "data", "value"]):
                    level_specific_recommendations.append(f"Level {level_number} ('{level}') uses generic naming - replace with business-meaningful term")
                
                # Check for technical naming patterns
                elif any(tech in level_clean for tech in ["id", "key", "code", "ref", "num"]):
                    level_specific_recommendations.append(f"Level {level_number} ('{level}') appears technical - consider business-friendly name")
                
                # Check for unclear abbreviations
                elif len(level.replace(" ", "")) <= 3 and level.isupper():
                    level_specific_recommendations.append(f"Level {level_number} ('{level}') abbreviation unclear - spell out full business term")
                
                # Check for appropriate level progression
                if j > 0:  # Not the first level
                    prev_level = levels[j-1].lower()
                    curr_level = level.lower()
                    
                    # Look for logical business progression patterns
                    progression_patterns = {
                        "division": ["department", "team", "group"],
                        "region": ["area", "territory", "district", "zone"],
                        "category": ["subcategory", "type", "class", "family"],
                        "year": ["quarter", "month", "week", "day"],
                        "company": ["division", "department", "team"],
                        "country": ["state", "region", "city", "location"],
                        "corp": ["div", "dept", "team"],
                        "territory": ["district", "store", "location"]
                    }
                    
                    has_logical_progression = False
                    for parent_term, child_terms in progression_patterns.items():
                        if parent_term in prev_level:
                            if any(child_term in curr_level for child_term in child_terms):
                                has_logical_progression = True
                                break
                    
                    if not has_logical_progression and j == 1:  # Second level check
                        level_specific_recommendations.append(f"Levels {level_number-1}-{level_number} ('{levels[j-1]}' > '{level}') lack clear business progression")
            
            # Overall structure analysis
            if len(levels) > 7:
                level_specific_recommendations.append(f"Too many levels ({len(levels)}) - consider consolidating to 4-6 levels for better usability")
            elif len(levels) < 3:
                level_specific_recommendations.append(f"Too few levels ({len(levels)}) - consider adding intermediate levels for better drill-down analysis")
            
            # Check for naming consistency
            naming_styles = []
            for level in levels:
                if "_" in level:
                    naming_styles.append("underscore")
                elif " " in level:
                    naming_styles.append("spaces")
                elif level.isupper():
                    naming_styles.append("uppercase")
                elif level.islower():
                    naming_styles.append("lowercase")
                else:
                    naming_styles.append("mixed_case")
            
            if len(set(naming_styles)) > 2:
                level_specific_recommendations.append("Inconsistent naming convention across levels - standardize to one style (e.g., 'Title Case')")
        
        # Display results
        print(f"   🔍 **Analysis Results:**")
        if level_specific_recommendations:
            for rec in level_specific_recommendations:
                print(f"      ⚠️  {rec}")
        else:
            print(f"      ✅ No specific level issues detected")
        
        # Check if detected issues match expectations
        detected_categories = []
        for rec in level_specific_recommendations:
            if "generic naming" in rec.lower():
                detected_categories.append("Generic naming")
            elif "technical" in rec.lower():
                detected_categories.append("Technical naming")  
            elif "abbreviation" in rec.lower():
                detected_categories.append("Unclear abbreviations")
            elif "too many" in rec.lower():
                detected_categories.append("Too many levels")
            elif "too few" in rec.lower():
                detected_categories.append("Too few levels")
            elif "inconsistent" in rec.lower():
                detected_categories.append("Inconsistent convention")
        
        # Validation
        print(f"   📊 **Validation:**")
        if not expected_issues and not detected_categories:
            print(f"      ✅ PASS: No issues expected or detected")
        elif set(expected_issues) & set(detected_categories):
            print(f"      ✅ PASS: Expected issues detected")
        else:
            print(f"      ❌ MISMATCH: Expected {expected_issues}, Got {detected_categories}")
        
        print()  # Blank line for readability
    
    print("=" * 70)
    print("📈 **Summary of Level-Specific Analysis Features:**")
    print()
    print("🎯 **Naming Quality Checks:**")
    print("   • Generic terms (Level1, Node, Item, Field, Data, Value)")
    print("   • Technical terms (ID, Key, Code, Ref, Num)")
    print("   • Unclear abbreviations (3 char uppercase: DIV, TM, PER)")
    print("   • Business meaningfulness of level names")
    print()
    print("🔗 **Business Logic Validation:**")
    print("   • Logical parent-child progression")
    print("   • Recognized business hierarchies (Region→Territory→District)")
    print("   • Semantic flow and business context")
    print()
    print("⚖️ **Structure Optimization:**")
    print("   • Optimal level count (3-6 levels)")
    print("   • Too many levels (>7): usability issues")
    print("   • Too few levels (<3): limited drill-down")
    print()
    print("📝 **Consistency Standards:**")
    print("   • Naming convention consistency")
    print("   • Style standardization across levels")
    print("   • Professional presentation")
    print()
    print("🎯 **Integration with Score Rationale:**")
    print("   • Top 3 level issues included in recommendations")
    print("   • Specific level numbers referenced for clarity")
    print("   • Actionable improvement suggestions provided")

if __name__ == "__main__":
    test_level_specific_analysis()