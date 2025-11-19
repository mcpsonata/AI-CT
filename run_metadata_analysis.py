#!/usr/bin/env python3
"""
Standalone Metadata Analysis Script
Runs metadata analysis using Python-based DAX expressions and generates Excel output
"""

import sys
import os
import json
from datetime import datetime
import pandas as pd

# Add the src directory to the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

# Import the server components
from server import TabularEditor, CopilotDataEvaluator

def run_metadata_analysis(workspace_name, dataset_name):
    """
    Run metadata analysis using the corrected DAX expressions
    
    Args:
        workspace_name: Name of the Power BI workspace
        dataset_name: Name of the dataset
    """
    print(f"🔍 Starting Metadata Analysis")
    print(f"📊 Workspace: {workspace_name}")
    print(f"📊 Dataset: {dataset_name}")
    
    try:
        # Step 1: Connect to dataset
        print("\n🔗 Step 1: Connecting to dataset...")
        tabular_editor = TabularEditor()
        connection_result = tabular_editor.connect_dataset(workspace_name, dataset_name)
        
        if "Connected successfully" not in str(connection_result):
            print(f"❌ Connection failed: {connection_result}")
            return
        
        print("✅ Connected successfully")
        
        # Step 2: Retrieve metadata using DAX expressions
        print("\n📊 Step 2: Retrieving metadata using DAX expressions...")
        
        # Get tables metadata using proper DMV syntax
        print("  Getting tables metadata...")
        tables_dax = """
        EVALUATE
        SELECTCOLUMNS(
            SYSTEMRESTRICTSCHEMA($System.TMSCHEMA_TABLES),
            "TableName", [Name],
            "Description", [Description]
        )
        """
        
        tables_result = tabular_editor.execute_dax_query(tables_dax)
        
        # Get columns metadata using proper DMV syntax
        print("  Getting columns metadata...")
        columns_dax = """
        EVALUATE
        SELECTCOLUMNS(
            SYSTEMRESTRICTSCHEMA($System.TMSCHEMA_COLUMNS),
            "TableName", [TableName],
            "ColumnName", [Name],
            "Description", [Description]
        )
        """
        
        columns_result = tabular_editor.execute_dax_query(columns_dax)
        
        # Get measures metadata using proper DMV syntax
        print("  Getting measures metadata...")
        measures_dax = """
        EVALUATE
        SELECTCOLUMNS(
            SYSTEMRESTRICTSCHEMA($System.TMSCHEMA_MEASURES),
            "TableName", [TableName],
            "MeasureName", [Name],
            "Description", [Description]
        )
        """
        
        measures_result = tabular_editor.execute_dax_query(measures_dax)
        
        # Step 3: Apply scoring logic
        print("\n🎯 Step 3: Applying scoring logic...")
        metadata_results = []
        
        # Process Tables
        print("  Processing tables...")
        for table_row in tables_result.get('data', []):
            table_name = table_row.get('TableName', '')
            description = table_row.get('Description', '') or ''
            
            # Scoring logic: 0.25 if has description, 0 if missing
            table_desc_score = 0.25 if description.strip() else 0.0
            has_description = 'Yes' if description.strip() else 'No'
            issue_severity = 'Good' if description.strip() else 'Critical'
            
            metadata_results.append({
                'Metadata_Type': 'Table',
                'Element_Name': table_name,
                'Table_Name': table_name,
                'Issue_Severity': issue_severity,
                'DAX_Expression': 'SYSTEMRESTRICTSCHEMA($System.TMSCHEMA_TABLES)',
                'Current_Description': description,
                'Has_Description': has_description,
                'Table_Description_Score': table_desc_score,
                'Column_Description_Score': 0.0,
                'Measure_Description_Score': 0.0,
                'Overall_Score': table_desc_score,
                'Max_Score': 0.25,
                'Recommendations': 'Add table description' if not description.strip() else 'Description present'
            })
        
        # Process Columns
        print("  Processing columns...")
        for column_row in columns_result.get('data', []):
            table_name = column_row.get('TableName', '')
            column_name = column_row.get('ColumnName', '')
            description = column_row.get('Description', '') or ''
            
            # Skip system columns
            if column_name.startswith('RowNumber-'):
                continue
            
            # Scoring logic: 0.25 if has description, 0 if missing
            column_desc_score = 0.25 if description.strip() else 0.0
            has_description = 'Yes' if description.strip() else 'No'
            issue_severity = 'Good' if description.strip() else 'Critical'
            
            metadata_results.append({
                'Metadata_Type': 'Column',
                'Element_Name': column_name,
                'Table_Name': table_name,
                'Issue_Severity': issue_severity,
                'DAX_Expression': 'SYSTEMRESTRICTSCHEMA($System.TMSCHEMA_COLUMNS)',
                'Current_Description': description,
                'Has_Description': has_description,
                'Table_Description_Score': 0.0,
                'Column_Description_Score': column_desc_score,
                'Measure_Description_Score': 0.0,
                'Overall_Score': column_desc_score,
                'Max_Score': 0.25,
                'Recommendations': 'Add column description' if not description.strip() else 'Description present'
            })
        
        # Process Measures
        print("  Processing measures...")
        for measure_row in measures_result.get('data', []):
            table_name = measure_row.get('TableName', '')
            measure_name = measure_row.get('MeasureName', '')
            description = measure_row.get('Description', '') or ''
            
            # Scoring logic: 0.25 if has description, 0 if missing
            measure_desc_score = 0.25 if description.strip() else 0.0
            has_description = 'Yes' if description.strip() else 'No'
            issue_severity = 'Good' if description.strip() else 'Critical'
            
            metadata_results.append({
                'Metadata_Type': 'Measure',
                'Element_Name': measure_name,
                'Table_Name': table_name,
                'Issue_Severity': issue_severity,
                'DAX_Expression': 'SYSTEMRESTRICTSCHEMA($System.TMSCHEMA_MEASURES)',
                'Current_Description': description,
                'Has_Description': has_description,
                'Table_Description_Score': 0.0,
                'Column_Description_Score': 0.0,
                'Measure_Description_Score': measure_desc_score,
                'Overall_Score': measure_desc_score,
                'Max_Score': 0.25,
                'Recommendations': 'Add measure description' if not description.strip() else 'Description present'
            })
        
        # Calculate summary statistics
        total_items = len(metadata_results)
        items_with_descriptions = sum(1 for r in metadata_results if r['Has_Description'] == 'Yes')
        total_score = sum(r['Overall_Score'] for r in metadata_results)
        max_possible_score = total_items * 0.25
        metadata_percentage = (total_score / max_possible_score * 100) if max_possible_score > 0 else 0
        
        print(f"\n📈 Analysis Summary:")
        print(f"  Total items analyzed: {total_items}")
        print(f"  Items with descriptions: {items_with_descriptions}")
        print(f"  Metadata coverage: {metadata_percentage:.1f}%")
        print(f"  Total score: {total_score:.2f}/{max_possible_score:.2f}")
        
        # Generate Excel file
        print("\n💾 Step 4: Generating Excel file...")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        excel_filename = f"Metadata_Analysis_{workspace_name.replace(' ', '')}_{dataset_name.replace(' ', '')}_{timestamp}.xlsx"
        
        # Ensure KnowledgeBase folder exists
        kb_folder = "KnowledgeBase"
        os.makedirs(kb_folder, exist_ok=True)
        excel_path = os.path.join(kb_folder, excel_filename)
        
        # Create DataFrame
        df = pd.DataFrame(metadata_results)
        
        # Create Excel with formatting
        try:
            from openpyxl import Workbook
            from openpyxl.styles import PatternFill, Font
            from openpyxl.utils.dataframe import dataframe_to_rows
            
            wb = Workbook()
            ws = wb.active
            ws.title = "Metadata Analysis"
            
            # Add summary
            summary_data = [
                ["METADATA ANALYSIS SUMMARY", "", "", "", "", "", "", "", "", "", "", "", ""],
                ["Analysis Timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "", "", "", "", "", "", "", "", "", "", ""],
                ["Workspace", workspace_name, "", "", "", "", "", "", "", "", "", "", ""],
                ["Dataset", dataset_name, "", "", "", "", "", "", "", "", "", "", ""],
                ["Total Items", str(total_items), "", "", "", "", "", "", "", "", "", "", ""],
                ["Items with Descriptions", str(items_with_descriptions), "", "", "", "", "", "", "", "", "", "", ""],
                ["Metadata Coverage %", f"{metadata_percentage:.1f}%", "", "", "", "", "", "", "", "", "", "", ""],
                ["", "", "", "", "", "", "", "", "", "", "", "", ""],
            ]
            
            for row_data in summary_data:
                ws.append(row_data)
            
            # Add data
            for r in dataframe_to_rows(df, index=False, header=True):
                ws.append(r)
            
            # Apply color coding
            severity_colors = {
                'Critical': PatternFill(start_color='FF6B6B', end_color='FF6B6B', fill_type='solid'),
                'Good': PatternFill(start_color='32CD32', end_color='32CD32', fill_type='solid')
            }
            
            # Find severity column and apply colors
            header_row = len(summary_data) + 1
            severity_col_index = None
            for col_idx, cell in enumerate(ws[header_row], 1):
                if cell.value == 'Issue_Severity':
                    severity_col_index = col_idx
                    break
            
            if severity_col_index:
                for row_idx in range(header_row + 1, ws.max_row + 1):
                    severity_cell = ws.cell(row=row_idx, column=severity_col_index)
                    severity_value = severity_cell.value
                    if severity_value in severity_colors:
                        for col_idx in range(1, ws.max_column + 1):
                            ws.cell(row=row_idx, column=col_idx).fill = severity_colors[severity_value]
            
            wb.save(excel_path)
            print(f"✅ Excel file saved: {excel_path}")
            
        except ImportError:
            # Fallback to basic Excel
            df.to_excel(excel_path, index=False, sheet_name='Metadata Analysis')
            print(f"✅ Basic Excel file saved: {excel_path}")
        
        print(f"\n🎉 Metadata Analysis completed successfully!")
        print(f"📁 Results saved to: {excel_path}")
        
        # Disconnect
        tabular_editor.disconnect_dataset()
        
    except Exception as e:
        print(f"❌ Error during metadata analysis: {str(e)}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    # Run the metadata analysis
    workspace_name = "MSXI-BilledPipeline02" 
    dataset_name = "BilledPipelineTrending"
    
    run_metadata_analysis(workspace_name, dataset_name)