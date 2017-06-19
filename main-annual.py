from pyomo.environ import *
import pandas as pd
from calvin import *

eop = None

for i in range(1922,2004):

  print('\nNow running WY %d' % i)
  print(eop)

  calvin = CALVIN('calvin/data/annual/linksWY%d.csv' % i, ic=eop)
  # calvin.inflow_multiplier(0.9)
  calvin.eop_constraint_multiplier(0.0)

  calvin.create_pyomo_model(debug_mode=True, debug_cost=2e6)
  calvin.solve_pyomo_model(solver='glpk', nproc=1, debug_mode=True, maxiter=15)
  # calvin.solve_pyomo_model(solver='cplex', nproc=32, debug_mode=True, maxiter=15)

  calvin.create_pyomo_model(debug_mode=False)
  calvin.solve_pyomo_model(solver='glpk', nproc=1, debug_mode=False)
  # calvin.solve_pyomo_model(solver='cplex', nproc=32, debug_mode=False)

  # this will append to results files
  eop = postprocess(calvin.df, calvin.model, 
                    resultdir='results-annual', annual=True) 

# this is only required as a separate step when running annual loop
aggregate_regions('results-annual')
