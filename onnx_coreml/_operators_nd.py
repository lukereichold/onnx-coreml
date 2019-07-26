from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import numpy as np
import copy

from typing import Sequence, Callable, List, Tuple, Optional, Text, Any
from coremltools.models.neural_network import NeuralNetworkBuilder  #type: ignore
from ._graph import Node, Graph
from coremltools.proto import NeuralNetwork_pb2 #type: ignore
from ._error_utils import ErrorHandling

from ._operators import _convert_relu

INT_MAX = 2**30

def _convert_concat(builder, node, graph, err):
    '''
    convert to CoreML ConcatND Layer:
    https://github.com/apple/coremltools/blob/655b3be5cc0d42c3c4fa49f0f0e4a93a26b3e492/mlmodel/format/NeuralNetwork.proto#L3521
    '''

    axis = node.attrs.get('axis')
    for i in range(len(node.inputs)):
        if node.inputs[i] in node.input_tensors and node.inputs[i] not in graph.constants_loaded:
            value = node.input_tensors[node.inputs[i]]
            builder.add_load_constant_nd(
                name=node.name + '_load_constant_' + str(i),
                output_name=node.inputs[i],
                constant_value=value,
                shape=[1] if value.shape == () else value.shape
            )
            graph.constants_loaded.add(node.inputs[i])

    builder.add_concat_nd(
        name=node.name,
        input_names=node.inputs,
        output_name=node.outputs[0],
        axis=axis
    )

def _convert_constant(builder, node, graph, err):
    '''
    convert to CoreML Load Constant ND Layer:
    https://github.com/apple/coremltools/blob/655b3be5cc0d42c3c4fa49f0f0e4a93a26b3e492/mlmodel/format/NeuralNetwork.proto#L3596
    '''

    value = node.attrs['value']
    # HACK: If Value is 0-Rank then make it 1-Rank
    builder.add_load_constant_nd(
        name=node.name,
        output_name=node.outputs[0],
        constant_value=value,
        shape=[1] if value.shape == () else value.shape
    )
    graph.constants_loaded(node.outputs[0])

def _convert_constant_of_shape(builder, node, graph, err):
    '''
    convert to CoreML Fill Static Layer:
    https://github.com/apple/coremltools/blob/655b3be5cc0d42c3c4fa49f0f0e4a93a26b3e492/mlmodel/format/NeuralNetwork.proto#L3641
    '''

    value = node.attrs.get('value', [0.0])
    # if shape is known, create tensor of given shape
    # otherwise create tensor at runtime
    if node.inputs[0] in node.input_tensors:
        output_shape = node.input_tensors[node.inputs[0]]
        # add_fill_static requires shape to be more than rank-1
        if len(output_shape.shape) == 1:
            output_shape = output_shape.reshape(output_shape.shape[0], 1)
        builder.add_fill_static(
            name=node.name,
            output_name=node.outputs[0],
            output_shape=output_shape,
            value=value[0]
        )
    else:
        builder.add_fill_dynamic(
            name=node.name,
            input_name=node.inputs[0],
            output_name=node.outputs[0],
            value=value[0]
        )

def _convert_gather(builder, node, graph, err):
    '''
    convert to CoreML Gather Along Axis Layer:
    https://github.com/apple/coremltools/blob/655b3be5cc0d42c3c4fa49f0f0e4a93a26b3e492/mlmodel/format/NeuralNetwork.proto#L4296
    '''
    axis = node.attrs.get('axis', 0)

    if len(node.inputs) != 2:
        err.unsupported_op_configuration(builder, node, graph, "Error in ONNX model: Gather expects two inputs")
    
    if node.inputs[0] in node.input_tensors and node.inputs[0] not in graph.constants_loaded:
        value = node.input_tensors[node.inputs[0]]
        builder.add_load_constant_nd(
            name=node.name + '_load_data',
            output_name=node.inputs[0],
            constant_value=value,
            shape=[1] if value.shape == () else value.shape
        )
        graph.constants_loaded.add(node.inputs[0])
    
    if node.inputs[1] in node.input_tensors and node.inputs[1] not in graph.constants_loaded:
        value = node.input_tensors[node.inputs[1]]
        builder.add_load_constant_nd(
            name=node.name+ '_load_indices',
            output_name=node.inputs[1],
            constant_value=value,
            shape=[1] if value.shape == () else value.shape
        )
        graph.constants_loaded.add(node.inputs[1])
    
    builder.add_gather(
        name=node.name,
        input_names=[node.inputs[0], node.inputs[1]],
        output_name=node.outputs[0],
        axis=axis
    )

def _convert_lstm(builder, node, graph, err):  # type: (NeuralNetworkBuilder, Node, Graph, ErrorHandling) -> None
    '''
    convert to CoreML Uni/Bi-Directional LSTM Layer:
    https://github.com/apple/coremltools/blob/655b3be5cc0d42c3c4fa49f0f0e4a93a26b3e492/mlmodel/format/NeuralNetwork.proto#L3282
    https://github.com/apple/coremltools/blob/655b3be5cc0d42c3c4fa49f0f0e4a93a26b3e492/mlmodel/format/NeuralNetwork.proto#L3348
    '''

    def get_weights(W, W_name, R, R_name, B):
        '''
        Helper routine to return weights in CoreML LSTM required format
        '''
        W = np.expand_dims(np.expand_dims(W, 3), 3)
        R = np.expand_dims(np.expand_dims(R, 3), 3)
    
        if W is None:
            err.missing_initializer(node,
                                    "Weight tensor: {} not found in the graph initializer".format(W_name))
        if R is None:
            err.missing_initializer(node,
                                    "Weight tensor: {} not found in the graph initializer".format(R_name))

        W_i, W_o, W_f, W_c = np.split(np.squeeze(W), 4)  #type: ignore
        R_i, R_o, R_f, R_c = np.split(np.squeeze(R), 4)  #type: ignore

        W_x = [W_i, W_f, W_o, W_c]
        W_h = [R_i, R_f, R_o, R_c]
        b = None
        if B is not None:
            b_Wi, b_Wo, b_Wf, b_Wc, b_Ri, b_Ro, b_Rf, b_Rc = np.split(np.squeeze(B), 8)  #type: ignore
            b = [b_Wi + b_Ri, b_Wf + b_Rf, b_Wo + b_Ro, b_Wc + b_Rc]

        return W_x, W_h, b

    def expand_dim(node_name, input_name, output_name, axes):
        builder.add_expand_dims(
            name=node_name,
            input_name=input_name,
            output_name=output_name,
            axes=axes
        )

    # Read attributes
    # activation alpha and beta
    if 'activation_alpha' in node.attrs or 'activation_beta' in node.attrs:
        err.unsupported_feature_warning(node, "Activation parameter alpha and beta are currently not used")
    
    inner_activation = 'SIGMOID'
    cell_state_update_activation = 'TANH'
    output_activation = 'TANH'

    if 'activations' in node.attrs:
        activations_list = node.attrs['activations']
    
        if len(activations_list) < 3:
            err.unsupported_op_configuration(builder, node, graph, "Error in ONNX model: Less number of activations provided")
    
        if len(activations_list) == 6:
            err.unsupported_feature_warning(node, "Forward and backward pass will use same activations.")

        inner_activation = activations_list[0]
        cell_state_update_activation = activations_list[1]
        output_activation = activations_list[2]
    
    # Provide max Clip Value if not provided
    clip_threshold = node.attrs.get('clip', 500000.0)

    # Extract direction from ONNX attribute
    direction = 1
    if 'direction' in node.attrs and node.attrs['direction'].decode('utf-8') == 'bidirectional':
        direction = 2

    hidden_size = node.attrs.get('hidden_size')

    input_forget = node.attrs.get('input_forget', 0) == 1

    # Read inputs
    W_name = node.inputs[1]
    R_name = node.inputs[2]
    B = None
    if len(node.inputs) > 3:
        B_name = node.inputs[3]
        B = node.input_tensors.get(B_name, None)
 
    W = node.input_tensors.get(W_name, None)
    R = node.input_tensors.get(R_name, None)

    W = np.split(W, direction)
    R = np.split(R, direction)
    if B is not None:
        B = np.split(B, direction)
    else:
        B = [None, None]

    # Get weights for forward direction
    W_x, W_h, b = get_weights(W[0], W_name, R[0], R_name, B[0])

    # shape of input
    input_size = W_x[0].shape[1]

    # Get input and output for hidden and cell  
    input_h = node.inputs[5] if len(node.inputs) > 5 else node.inputs[0] + '_h_input'
    input_c = node.inputs[6] if len(node.inputs) > 6 else node.inputs[0] + '_c_input'
    output_h = node.outputs[1] if len(node.outputs) > 1 else node.outputs[0] + '_h_output'
    output_c = node.outputs[2] if len(node.outputs) > 2 else node.outputs[0] + '_c_output'
    output_h_5d = output_h + '_5d'
    output_c_5d = output_c + '_5d'

    # if input is not present in the network, load they as constant
    if node.inputs[0] not in graph.shape_dict:
        err.unsupported_op_configuration(builder, node, graph, "Input shape not represented within Graph")
    
    # Input is represented as [Seq Len, Batch Size, Input Size]
    batch_size = graph.shape_dict[node.inputs[0]][1]
    if len(node.inputs) < 6:
        builder.add_load_constant_nd(
            name=node.name + '_load_initial_h_and_c',
            output_name=input_h,
            constant_value=0.0,
            shape=[direction, batch_size, hidden_size]
        )
        # OPTIMIZATION: let's reuse the intial weights
        input_c = input_h

    # Get tensors for peepholes
    peepholes = node.inputs[7] if len(node.inputs) > 7 else None

    # CoreML LSTM expects 5-d tensor
    # Expand dimensions of input to 5-d for compatibility
    if len(graph.shape_dict[node.inputs[0]]) < 5:
        total_dims = len(graph.shape_dict[node.inputs[0]])
        add_nodes = 5 - total_dims
        
        expand_dim(node.name+'_expand_in_0', node.inputs[0], node.inputs[0]+'_expand_out_0', [total_dims])
        expand_dim(node.name+'_expand_in_h_0', input_h, input_h+'_expand_out_h_0', [total_dims])
        expand_dim(node.name+'_expand_in_c_0', input_c, input_c+'_expand_out_c_0', [total_dims])

        for i in range(1, add_nodes):
            i_str = str(i)
            i_p_str = str(i-1)
            expand_dim(node.name+'_expand_in_'+i_str, node.inputs[0]+'_expand_out_'+i_p_str, node.inputs[0]+'_expand_out_'+i_str, [total_dims+i])
            expand_dim(node.name+'_expand_in_h_'+i_str, input_h+'_expand_out_h_'+i_p_str, input_h+'_expand_out_h_'+i_str, [total_dims+i])
            expand_dim(node.name+'_expand_in_c_'+i_str, input_c+'_expand_out_c_'+i_p_str, input_c+'_expand_out_c_'+i_str, [total_dims+i])

    if direction == 1:
        # Peephole from ONNX are of shape [Num Dir, 3 * hidden_size]
        # Reshape into CoreML format of [input hs, forget hs, cell hs]
        if peepholes is not None:
            builder.add_reshape_static(
                name=node.name + '_peephole_reshape',
                input_name=peepholes,
                output_name=peepholes+'_reshaped',
                output_shape=[hidden_size, hidden_size, hidden_size]
            )
            peepholes = peepholes + '_reshaped'

        builder.add_unilstm(
            name=node.name,
            W_h=W_h,
            W_x=W_x,
            b=b,
            hidden_size=hidden_size,
            input_size=input_size,
            input_names=[node.inputs[0] + '_expand_out_' + str(add_nodes-1), input_h + '_expand_out_h_' + str(add_nodes-1), input_c + '_expand_out_c_' + str(add_nodes-1)],
            output_names=[node.outputs[0]+'_5d_out', output_h_5d, output_c_5d],
            inner_activation=inner_activation,
            cell_state_update_activation=cell_state_update_activation,
            output_activation=output_activation,
            peep=peepholes,
            output_all=True,
            forget_bias=True,
            coupled_input_forget_gate=input_forget,
            cell_clip_threshold=clip_threshold,
            reverse_input=False
        )
    elif direction == 2:
        if len(W) != 2 and len(R) != 2 and len(B) != 2:
            err.unsupported_op_configuration(builder, node, graph, "Bi-Directional LSTM does not have weights for both the directions")

        W_x_back, W_h_back, b_back = get_weights(W[1], W_name, R[1], R_name, B[1])

        peephole_f = None
        peephole_b = None
        if peepholes is not None:
            builder.add_reshape_static(
                name=node.name + '_peephole_reshape',
                input_name=peepholes,
                output_name=peepholes+'_reshaped',
                output_shape=[direction, hidden_size, hidden_size, hidden_size]
            )

            peepholes_f = peepholes + '_f'
            peepholes_b = peepholes + '_b'

            builder.add_split_nd(
                name=node.name+'_peephole_split',
                input_name=peepholes+'_reshaped',
                output_names=[peepholes_f, peepholes_b],
                axis=0
            )

        # split input_h and input_c into two parts
        builder.add_split_nd(
            name=node.name+'_split_h',
            input_name=input_h+'_expand_out_h_' + str(add_nodes-1),
            output_names=[input_h+'_f', input_h+'_b'],
            axis=0
        )

        # OPTIMIZATION: If input_h and input_c are same
        # Avoid creating new split and instead reuse
        if input_h != input_c:
            builder.add_split_nd(
                name=node.name+'_split_c',
                input_name=input_c+'_expand_out_c_' + str(add_nodes-1),
                output_names=[input_c+'_f', input_c+'_b'],
                axis=0
            )

        builder.add_bidirlstm(
            name=node.name,
            W_h=W_h,
            W_x=W_x,
            b=b,
            W_h_back=W_h_back,
            W_x_back=W_x_back,
            b_back=b_back,
            hidden_size=hidden_size,
            input_size=input_size,
            input_names=[node.inputs[0] + '_expand_out_' + str(add_nodes-1), input_h+'_f', input_c+'_f', input_h+'_b', input_c+'_b'],
            output_names=[node.outputs[0]+'_5d_out', output_h+'_f', output_c+'_f', output_h+'_b', output_c+'_b'],
            inner_activation=inner_activation,
            cell_state_update_activation=cell_state_update_activation,
            output_activation=output_activation,
            output_all=True,
            peep=peephole_f,
            peep_back=peephole_b,
            forget_bias=True,
            coupled_input_forget_gate=input_forget,
            cell_clip_threshold=clip_threshold
        )
                
        # Combine output_h and output_c
        builder.add_concat_nd(
            name=node.name+'concat_output_h',
            input_names=[output_h+'_f', output_h+'_b'],
            output_name=output_h_5d,
            axis=0
        )

        builder.add_concat_nd(
            name=node.name+'concat_output_c',
            input_names=[output_c+'_f', output_c+'_b'],
            output_name=output_c_5d,
            axis=0
        )
    else:
        err.unsupported_op_configuration(builder, node, graph, "Unsupported direction {} for LSTM".format(direction))


    # CoreML output is [Seq Len, Batch Size, Num Dir * Hidden Size, 1, 1]
    # Return output as [Seq Len, Num Dir, Batch Size, Hidden Size]
    # Following steps:
    #       a. Reshape and split hidden size for direction [Seq Len, Batch Size, Num Dir, Hidden Size, 1]
    #       b. Squeeze last dimension [Seq Len, Batch Size, Num Dir, Hidden Size]
    #       c. Permute to fix the order [Seq Len, Num Dir, Batch Size, Hidden Size, 1]
    builder.add_rank_preserving_reshape(
        name=node.name + '_reshape_',
        input_name=node.outputs[0]+'_5d_out',
        output_name=node.outputs[0]+'_5d_reshaped',
        output_shape=[0, 0, direction, -1, 0]
    )

    builder.add_squeeze(
        name=node.name+'_squeeze_out',
        input_name=node.outputs[0]+'_5d_reshaped',
        output_name=node.outputs[0]+'_4d',
        axes=[-1]
    )

    builder.add_transpose(
        name=node.name + '_transpose',
        axes=[0, 2, 1, 3],
        input_name=node.outputs[0] + '_4d',
        output_name=node.outputs[0]
    )

    # Squeeze dimensions of output_h and output_c
    builder.add_squeeze(
        name=node.name+'_squeeze_out_h',
        input_name=output_h_5d,
        output_name=output_h,
        axes=[-1, -2]
    )
    builder.add_squeeze(
        name=node.name+'_squeeze_out_c',
        input_name=output_c_5d,
        output_name=output_c,
        axes=[-1, -2]
    )

def _convert_matmul(builder, node, graph, err):
    '''
    convert to CoreML BatchedMatMul Layer:
    https://github.com/apple/coremltools/blob/655b3be5cc0d42c3c4fa49f0f0e4a93a26b3e492/mlmodel/format/NeuralNetwork.proto#L3473
    '''

    weight_name = node.inputs[1]
    W = None
    weight_as_layer_parameter = False
    if weight_name in node.input_tensors:
        W = node.input_tensors[weight_name]

    if W is not None:
        if len(W.shape) != 2:
            # since weight as parameter in batchedMatMul layer must be rank 2
            builder.add_load_constant_nd(node.name + '_const_weight_input', weight_name, constant_value=W,shape=W.shape)
        else:
            weight_as_layer_parameter = True

    if weight_as_layer_parameter:
        builder.add_batched_mat_mul(name=node.name,
                                    input_names=[node.inputs[0]],
                                    output_name=node.outputs[0],
                                    weight_matrix_rows=W.shape[0],
                                    weight_matrix_columns=W.shape[1],
                                    W=W)
    else:
        builder.add_batched_mat_mul(name=node.name,
                                    input_names=[node.inputs[0], weight_name],
                                    output_name=node.outputs[0])


def _convert_reshape(builder, node, graph, err):
    '''
    convert to CoreML Reshape Static Layer:
    https://github.com/apple/coremltools/blob/655b3be5cc0d42c3c4fa49f0f0e4a93a26b3e492/mlmodel/format/NeuralNetwork.proto#L4844
    '''
    shape_node = node.inputs[1]
    if shape_node in node.input_tensors:
        output_shape = node.input_tensors[shape_node]
    
        # if rank is same, then call rank preserving reshape
        if node.inputs[0] not in graph.shape_dict:
            err.unsupported_op_configuration(builder, node, graph, "Input shape not represented in graph")
    
        len_of_input_shape = len(graph.shape_dict[node.inputs[0]])
        if len(output_shape) == len_of_input_shape:
            builder.add_rank_preserving_reshape(
                name=node.name,
                input_name=node.inputs[0],
                output_name=node.outputs[0],
                output_shape=output_shape
            )
        else:
            add_static_reshape = True
            if len_of_input_shape > len(output_shape):
                num_zeros = 0
                num_neg_ones = 0
                for i in output_shape:
                    if i == 0:
                        num_zeros += 1
                    elif i == -1:
                        num_neg_ones += 1

                if num_neg_ones > 1:
                     err.unsupported_op_configuration(builder, node, graph, "Error in ONNX model: At most one dimension of new shape can be -1, found {}".format(num_neg_ones))

                if num_neg_ones + num_zeros == len(output_shape):
                    # Rank of output is less than input
                    # Make Rank equivalent for reshape and then squeeze
                    add_static_reshape = False
                    new_shape = []
                    i = 0
                    for i in range(len(output_shape)):
                        new_shape.append(output_shape[i])
                        if output_shape[i] == -1:
                            break
                    while i < len_of_input_shape-1:
                        new_shape.append(1)
                        i += 1

                    builder.add_rank_preserving_reshape(
                        name=node.name + '_reshape_preserving',
                        input_name=node.inputs[0],
                        output_name=node.outputs[0] + '_reshape_dim_preserved',
                        output_shape=new_shape
                    )

                    squeeze_axes = list(range(len(output_shape) - len_of_input_shape, 0))
                    squeeze_axes.reverse()

                    builder.add_squeeze(
                        name=node.name,
                        input_name=node.outputs[0] + '_reshape_dim_preserved',
                        output_name=node.outputs[0],
                        axes=squeeze_axes
                    )

            if add_static_reshape:    
                builder.add_reshape_static(
                    name=node.name,
                    input_name=node.inputs[0],
                    output_name=node.outputs[0],
                    output_shape=output_shape
                )
    else:
        builder.add_reshape_dynamic(
            name=node.name,
            input_names=node.inputs,
            output_name=node.outputs[0],
        )

def _convert_slice(builder, node, graph, err):
    '''
    convert to CoreML Slice Static Layer:
    https://github.com/apple/coremltools/blob/655b3be5cc0d42c3c4fa49f0f0e4a93a26b3e492/mlmodel/format/NeuralNetwork.proto#L5082
    ''' 
    
    data_shape = graph.shape_dict[node.inputs[0]]
    len_of_data = len(data_shape)
    begin_masks = [True] * len_of_data
    end_masks = [True] * len_of_data

    default_axes = list(range(len_of_data))
    default_steps = [1] * len_of_data
    
    ip_starts = node.attrs.get('starts')
    ip_ends = node.attrs.get('ends')
    axes = node.attrs.get('axes', default_axes)
    steps = node.attrs.get('steps', default_steps)

    starts = [0] * len_of_data
    ends = [0] * len_of_data

    for i in range(len(axes)):
        current_axes = axes[i]
        starts[current_axes] = ip_starts[i]
        ends[current_axes] = ip_ends[i]
        if ends[current_axes] != INT_MAX or ends[current_axes] < data_shape[current_axes]:
            end_masks[current_axes] = False

        if starts[current_axes] != 0:
            begin_masks[current_axes] = False

    builder.add_slice_static(
        name=node.name,
        input_name=node.inputs[0],
        output_name=node.outputs[0],
        begin_ids=starts,
        end_ids=ends,
        strides=steps,
        begin_masks=begin_masks,
        end_masks=end_masks
    )

def _convert_split(builder, node, graph, err):
    '''
    convert to CoreML Squeeze Layer:
    https://github.com/apple/coremltools/blob/655b3be5cc0d42c3c4fa49f0f0e4a93a26b3e492/mlmodel/format/NeuralNetwork.proto#5003
    '''

    axis = node.attrs.get('axis', 0)
    builder.add_split_nd(
        name=node.name,
        input_name=node.inputs[0],
        output_names=node.outputs,
        axis=axis
    )

def _convert_squeeze(builder, node, graph, err):
    '''
    convert to CoreML Squeeze Layer:
    https://github.com/apple/coremltools/blob/655b3be5cc0d42c3c4fa49f0f0e4a93a26b3e492/mlmodel/format/NeuralNetwork.proto#L4903
    '''
    axes = node.attrs.get('axes', None)
    builder.add_squeeze(
        name=node.name,
        input_name=node.inputs[0],
        output_name=node.outputs[0],
        axes=axes
    )

def _convert_shape(builder, node, graph, err):
    '''
    convert to CoreML GetShape Layer:
    https://github.com/apple/coremltools/blob/655b3be5cc0d42c3c4fa49f0f0e4a93a26b3e492/mlmodel/format/NeuralNetwork.proto#L5131
    '''
    builder.add_get_shape(
        name=node.name,
        input_name=node.inputs[0],
        output_name=node.outputs[0]
    )

def _convert_transpose(builder, node, graph, err):
    '''
    convert to CoreML Transpose Layer:
    https://github.com/apple/coremltools/blob/655b3be5cc0d42c3c4fa49f0f0e4a93a26b3e492/mlmodel/format/NeuralNetwork.proto#L3426
    '''
    
    axes = node.attrs.get('perm', [])
    # If 'perm' not provided, the reverse the dimensions
    if axes == []:
        rank = len(graph.shape_dict[node.inputs[0]])
        axes = list(range(-1, -(rank+1), -1))

    builder.add_transpose(
        name=node.name,
        axes=axes,
        input_name=node.inputs[0],
        output_name=node.outputs[0]
    )

def _convert_unsqueeze(builder, node, graph, err):
    '''
    convert to CoreML ExpandDim Layer:
    https://github.com/apple/coremltools/blob/655b3be5cc0d42c3c4fa49f0f0e4a93a26b3e492/mlmodel/format/NeuralNetwork.proto#L4810
    '''
    axes = node.attrs.get('axes')
    builder.add_expand_dims(
        name=node.name,
        input_name=node.inputs[0],
        output_name=node.outputs[0],
        axes=axes
    )


_ONNX_NODE_REGISTRY_ND = {
    "Concat": _convert_concat,
    "Constant": _convert_constant,
    "ConstantOfShape": _convert_constant_of_shape,
    "Gather": _convert_gather,
    "LSTM": _convert_lstm,
    "MatMul": _convert_matmul,
    "Relu": _convert_relu,
    "Reshape": _convert_reshape,
    "Slice": _convert_slice,
    "Split": _convert_split,
    "Shape": _convert_shape,
    "Squeeze": _convert_squeeze,
    "Transpose": _convert_transpose,
    "Unsqueeze": _convert_unsqueeze
}

def _get_node_converter_fn(builder, node, err):  # type: (NeuralNetworkBuilder, Node, ErrorHandling) -> Callable[[NeuralNetworkBuilder, Node, Graph, ErrorHandling], None]
    """
    Get the right converter function for ONNX node op_type
    """
    op_type = node.op_type
    if op_type in _ONNX_NODE_REGISTRY_ND:
        return _ONNX_NODE_REGISTRY_ND[op_type]
    else:
        return err.unsupported_op(node)

def _convert_node_nd(builder, node, graph, err):  # type: (NeuralNetworkBuilder, Node, Graph, ErrorHandling) -> None
    converter_fn = _get_node_converter_fn(builder, node, err)
    return converter_fn(builder, node, graph, err)
