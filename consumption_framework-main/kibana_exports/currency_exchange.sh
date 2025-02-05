#!/bin/bash

# Script to create new on-prem CF dashboard w/ currency

# Prompt user for the currency
read -p "Enter on-prem costs currency: " currency

# Paths to the input and output files (set accordingly)
input_file="kibana_exports/8.13.1-onprem-USD.ndjson"
output_file="kibana_exports/8.13.1-onprem-exchanged.ndjson"

# Check if input file exists
if [ ! -f "$input_file" ]; then
  echo "Input file '$input_file' not found!"
  exit 1
fi

# Clear output file if it already exists
> "$output_file"

# Perform find and replace
sed "s/USD/$currency/g" "$input_file" > "$output_file"

echo "Updated dashboarding file has been saved to $output_file"
